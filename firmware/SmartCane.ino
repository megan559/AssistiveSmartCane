#include <WiFi.h>
#include <HTTPClient.h>
#include <ESPmDNS.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// ---------------- WIFI ----------------
const char* ssid = "";
const char* password = "";

// Server is found by name via mDNS (server.py advertises "assistant.local").
// Filled in at boot after WiFi connects.
String serverUrl = "";       // /update -- button POSTs here
String serverNavUrl = "";    // /nav_step -- LCD polls here

// LCD navigation state. Only redraw when the displayed content actually
// changes, so the screen doesn't flicker every poll.
String lastDistance = "";
String lastInstruction = "";
bool navActive = false;

// Non-blocking poll timer for the nav-step endpoint.
unsigned long lastNavPoll = 0;
const unsigned long NAV_POLL_MS = 3000;   // every 3 seconds

// ---------------- PINS ----------------
#define TRIG_PIN 5
#define ECHO_PIN 18
#define BUTTON_PIN 4
#define BUZZER_PIN 13    // active buzzer, beep RATE encodes proximity

LiquidCrystal_I2C lcd(0x27, 16, 2);

long duration;
float distance;

// button state tracking
bool lastButtonRead = HIGH;

// ---------------- BUZZER (proximity-proportional beeping) ----------------
// Non-blocking: we never delay() in the buzzer code, we just toggle a
// pin on a millis()-based interval that gets SHORTER as the obstacle
// gets closer. At <~5 cm it's effectively continuous.
unsigned long buzzerLastToggle = 0;
bool buzzerState = false;
const float OBSTACLE_FAR_CM   = 100.0;  // beyond this -> silent
const float OBSTACLE_NEAR_CM  = 5.0;    // closer than this -> max urgency
const unsigned long BEEP_INTERVAL_FAR_MS  = 800;
const unsigned long BEEP_INTERVAL_NEAR_MS = 80;

// ---------------- RESOLVE SERVER BY mDNS ----------------
void resolveServer() {
  IPAddress serverIP;
  // MDNS.queryHost("assistant") resolves "assistant.local"
  while (serverIP.toString() == "0.0.0.0") {
    serverIP = MDNS.queryHost("assistant");
    if (serverIP.toString() == "0.0.0.0") {
      Serial.println("Resolving assistant.local...");
      lcd.clear();
      lcd.print("Finding server");
      delay(1000);
    }
  }
  serverUrl    = "http://" + serverIP.toString() + ":5000/update";
  serverNavUrl = "http://" + serverIP.toString() + ":5000/nav_step";
  Serial.println("Server: " + serverUrl);
  lcd.clear();
  lcd.print("Server found");
  delay(800);
  lcd.clear();
}

void setup() {
  Serial.begin(115200);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);   // ensure quiet at boot

  lcd.init();
  lcd.backlight();
  lcd.print("Connecting WiFi");

  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  lcd.clear();
  lcd.print("WiFi Ready");
  delay(1000);
  lcd.clear();

  // start mDNS so we can resolve the server's hostname
  if (!MDNS.begin("esp32-button")) {
    Serial.println("mDNS init failed");
  }

  resolveServer();
}

void sendPost(String state, float dist) {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.begin(serverUrl.c_str());
  http.addHeader("Content-Type", "application/json");

  String json = "{";
  json += "\"status\":\"" + state + "\",";
  json += "\"distance\":" + String(dist);
  json += "}";

  int code = http.POST(json);
  http.end();
  Serial.println("Sent: " + json + "  -> " + String(code));

  // If the POST failed (server IP changed, e.g. new network), re-resolve.
  if (code <= 0) {
    Serial.println("POST failed, re-resolving server...");
    serverUrl = "";
    resolveServer();
  }
}

// Extract a single string field from a flat JSON body, e.g. "instruction".
// Tiny, no library, good enough for the small known shape /nav_step returns.
String jsonStr(const String& body, const String& key) {
  String pattern = "\"" + key + "\":\"";
  int i = body.indexOf(pattern);
  if (i < 0) return "";
  i += pattern.length();
  int j = body.indexOf('"', i);
  if (j < 0) return "";
  return body.substring(i, j);
}

bool jsonBool(const String& body, const String& key) {
  // looks for "key":true (no quotes around the value)
  String pattern = "\"" + key + "\":true";
  return body.indexOf(pattern) >= 0;
}

void clearLcdNav() {
  if (navActive || lastInstruction.length() || lastDistance.length()) {
    lcd.clear();
    navActive = false;
    lastInstruction = "";
    lastDistance = "";
  }
}

// LCD shows the destination, not turn-by-turn. Cleaner on a 16x2,
// no flicker as steps advance, immediately readable to an observer.
// Only redraw if the destination actually changed.
void showOnLcd(const String& destination) {
  if (destination == lastInstruction) return;   // reuse lastInstruction as cache

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Going to:");
  lcd.setCursor(0, 1);
  lcd.print(destination.substring(0, 16));

  lastInstruction = destination;
  navActive = true;
}

void fetchNavStep() {
  if (serverNavUrl.length() == 0) return;        // server not resolved yet
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.setTimeout(4000);
  http.begin(serverNavUrl);
  int code = http.GET();
  if (code == 200) {
    String body = http.getString();
    if (jsonBool(body, "active")) {
      String dest = jsonStr(body, "destination");
      showOnLcd(dest);
    } else {
      clearLcdNav();
    }
  } else {
    // network blip; leave the LCD as-is rather than flickering
    Serial.printf("nav_step HTTP %d\n", code);
  }
  http.end();
}

// Decide whether the buzzer should be ON or OFF right now, based on the
// most recent ultrasonic distance. Called every loop() iteration; uses
// millis()-based timing so it never blocks.
void updateBuzzer(float distance_cm) {
  // 0 from pulseIn means "no echo" -> nothing in range -> silent.
  // Also silence anything beyond the far threshold.
  if (distance_cm <= 0 || distance_cm > OBSTACLE_FAR_CM) {
    if (buzzerState) {
      digitalWrite(BUZZER_PIN, LOW);
      buzzerState = false;
    }
    return;
  }

  // Map distance to beep interval: closer = shorter interval.
  float d = distance_cm;
  if (d < OBSTACLE_NEAR_CM) d = OBSTACLE_NEAR_CM;
  float t = (d - OBSTACLE_NEAR_CM) / (OBSTACLE_FAR_CM - OBSTACLE_NEAR_CM);
  unsigned long interval = BEEP_INTERVAL_NEAR_MS +
      (unsigned long)(t * (BEEP_INTERVAL_FAR_MS - BEEP_INTERVAL_NEAR_MS));

  if (millis() - buzzerLastToggle >= interval) {
    buzzerLastToggle = millis();
    buzzerState = !buzzerState;
    digitalWrite(BUZZER_PIN, buzzerState ? HIGH : LOW);
  }
}

void loop() {
  // ---------------- ULTRASONIC ----------------
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  duration = pulseIn(ECHO_PIN, HIGH, 30000);
  distance = duration * 0.034 / 2;

  // ---------------- OBSTACLE ALERT (buzzer) ----------------
  // Always active, independent of navigation state: this is the
  // safety/orientation function and it should never go silent just
  // because nav isn't running.
  updateBuzzer(distance);

  // ---------------- BUTTON EDGE DETECTION ----------------
  bool currentRead = digitalRead(BUTTON_PIN);

  // detect PRESS event (HIGH -> LOW)
  if (lastButtonRead == HIGH && currentRead == LOW) {
    sendPost("PRESSED", distance);
    // Only briefly show "Button Pressed" if we're NOT mid-navigation
    // -- otherwise it would clobber the live turn display and flicker.
    if (!navActive) {
      lcd.clear();
      lcd.setCursor(0, 0);
      lcd.print("Button Pressed");
      lcd.setCursor(0, 1);
      lcd.print(distance);
    }
    delay(200); // debounce
  }

  // ---------------- LIVE NAVIGATION POLL ----------------
  // Every NAV_POLL_MS, fetch the current nav step and update the LCD.
  // Non-blocking: we never sleep here, just check the clock.
  if (millis() - lastNavPoll >= NAV_POLL_MS) {
    lastNavPoll = millis();
    fetchNavStep();
  }

  lastButtonRead = currentRead;
  delay(50);
}

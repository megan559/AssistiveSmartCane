# 🦯 Smart Cane
### *Seeing the world through sound, sensors, and a little bit of AI.*

> "The white cane has been the symbol of independence for the visually impaired for over a century. We didn't want to replace it — we wanted to give it a brain."

---

## 👁️ The Idea

Every year, millions of visually impaired people navigate the world using nothing but a cane and their instincts. It works — but it's reactive, not predictive. **Smart Cane** reimagines this everyday tool as an intelligent companion: one that senses obstacles before they're touched, understands context through AI, and guides users to where they actually want to go.

No new habits to learn. No bulky wearables. Just a cane that quietly got smarter.

---

## ✨ What It Does

| Capability | How |
|---|---|
| 🚧 **Obstacle Detection** | Ultrasonic sensing detects objects in real time and alerts the user before contact |
| 🧠 **Intelligent Assistance** | Gemini API interprets context and provides smart, conversational guidance |
| 🗺️ **Turn-by-Turn Navigation** | Google Directions API calculates safe walking routes on the fly |
| 📱 **Companion App** | A lightweight Android app pairs with the cane for live feedback and control |
| ⚡ **Real-Time Communication** | A Flask backend keeps hardware, AI, and app perfectly in sync |

---

## 🏗️ How It's Built

```
        ┌─────────────────┐
        │   HC-SR04       │   senses the world
        │  (Ultrasonic)   │
        └────────┬────────┘
                 │
        ┌────────▼────────┐
        │      ESP32       │   thinks fast, acts faster
        │  (Microcontroller)│
        └────────┬────────┘
                 │  Wi-Fi
        ┌────────▼────────┐
        │   Flask Server   │   the nervous system
        └───┬─────────┬────┘
            │         │
   ┌────────▼──┐   ┌──▼─────────────┐
   │ Gemini API │   │ Google Directions│
   │  (context) │   │     (routes)     │
   └────────────┘   └──────────────────┘
                 │
        ┌────────▼────────┐
        │   Android App    │   the user's window in
        └─────────────────┘
```

---

## 🛠️ Tech Stack

- **Hardware:** ESP32, HC-SR04 Ultrasonic Sensor
- **Backend:** Python, Flask
- **AI:** Gemini API
- **Navigation:** Google Directions API
- **Mobile:** Android (Java/Python)

---

## 🚀 Getting Started

```bash
# Clone the repository
git clone https://github.com/megan559/smart-cane.git
cd smart-cane

# Set up the backend
cd backend
pip install -r requirements.txt
python app.py

# Flash the ESP32 firmware
# (see /firmware for wiring diagram + upload instructions)

# Build and run the Android app
# Open /android in Android Studio and run on device/emulator
```

> 📌 You'll need API keys for Gemini and Google Directions — add them to a `.env` file in `/backend` (see `.env.example`).

---

## 🎯 Why It Matters

Assistive tech shouldn't feel like tech — it should feel invisible. Smart Cane was built on the belief that independence isn't about adding more devices to someone's life, it's about making the one tool they already trust a little bit smarter.

---

## 🔭 What's Next

- [ ] Voice-command interaction
- [ ] Haptic feedback patterns for obstacle direction
- [ ] Offline mode for low-connectivity areas
- [ ] Battery optimization for all-day wear

---

## 👩‍💻 Built By

**Megan Joanna Pinto**
BSc Information Technology Graduate — Amity University Dubai
📧 pintomegan4@gmail.com | 🔗 [LinkedIn](https://linkedin.com/in/megan-joanna/) | 💻 [GitHub](https://github.com/megan559)

---

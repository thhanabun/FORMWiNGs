#include "Arduino_BMI270_BMM150.h"

// Ready-to-upload sender for Arduino Nano 33 BLE Sense Rev2.
// Only the waist-mounted BMI270 accelerometer/gyroscope channels are sent.
// Output follows IMU data-ready events; configure a driver/ODR supporting
// 200 Hz when that capture target is required.

constexpr unsigned long BAUD_RATE = 460800;
constexpr unsigned long PERIOD_US = 5000;
constexpr int FEEDBACK_PIN = LED_BUILTIN;  // Replace with a motor/buzzer driver input pin.
uint32_t previousMicros = 0;
uint64_t elapsedMicros = 0;
uint32_t sequence = 0;
unsigned long feedbackUntilMs = 0;
String incomingFeedback;

uint16_t crc16(const String &payload) {
  uint16_t crc = 0xFFFF;
  for (size_t index = 0; index < payload.length(); ++index) {
    crc ^= static_cast<uint16_t>(payload[index]) << 8;
    for (int bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021) : static_cast<uint16_t>(crc << 1);
    }
  }
  return crc;
}

void setup() {
  Serial.begin(BAUD_RATE);
  while (!Serial) {}
  pinMode(FEEDBACK_PIN, OUTPUT);
  if (!IMU.begin()) {
    Serial.println("#ERROR,IMU_INIT_FAILED");
    while (true) {}
  }
}

void serviceFeedback() {
  while (Serial.available()) {
    char character = static_cast<char>(Serial.read());
    if (character == '\n') {
      if (incomingFeedback.startsWith("@ALERT,ALERT")) {
        feedbackUntilMs = millis() + 1500;
      } else if (incomingFeedback.startsWith("@ALERT,WARN")) {
        feedbackUntilMs = millis() + 400;
      }
      incomingFeedback = "";
    } else if (incomingFeedback.length() < 128) {
      incomingFeedback += character;
    }
  }
  digitalWrite(FEEDBACK_PIN, millis() < feedbackUntilMs ? HIGH : LOW);
}

void loop() {
  serviceFeedback();
  uint32_t now = micros();
  uint32_t delta = now - previousMicros;
  if (delta < PERIOD_US || !IMU.accelerationAvailable() || !IMU.gyroscopeAvailable()) {
    return;
  }
  previousMicros = now;
  elapsedMicros += delta;
  float ax, ay, az, gx, gy, gz;
  IMU.readAcceleration(ax, ay, az);
  IMU.readGyroscope(gx, gy, gz);
  String payload = "@IMU," + String(sequence++) + "," + String(elapsedMicros / 1000000.0, 4) + "," +
    String(ax, 5) + "," + String(ay, 5) + "," + String(az, 5) + "," +
    String(gx, 4) + "," + String(gy, 4) + "," + String(gz, 4);
  char crcText[5];
  snprintf(crcText, sizeof(crcText), "%04X", crc16(payload));
  Serial.print(payload);
  Serial.print("*");
  Serial.println(crcText);
}

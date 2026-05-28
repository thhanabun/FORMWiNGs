// Arduino Nano waist-worn accelerometer/gyroscope transmitter template.
// Configure the actual IMU for 200 Hz before collection. Keep raw measurements
// here; calibration and filtering run in the recorder for reproducible training.

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

void readImu(float &ax, float &ay, float &az, float &gx, float &gy, float &gz) {
  // Replace this stationary demo value with accelerometer/gyroscope reads.
  ax = 0.0; ay = 0.0; az = 1.0;
  gx = 0.0; gy = 0.0; gz = 0.0;
}

void setup() {
  Serial.begin(BAUD_RATE);
  pinMode(FEEDBACK_PIN, OUTPUT);
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
  if (delta < PERIOD_US) {
    return;
  }
  previousMicros = now;
  elapsedMicros += delta;
  float ax, ay, az, gx, gy, gz;
  readImu(ax, ay, az, gx, gy, gz);
  String payload = "@IMU," + String(sequence++) + "," + String(elapsedMicros / 1000000.0, 4) + "," +
    String(ax, 5) + "," + String(ay, 5) + "," + String(az, 5) + "," +
    String(gx, 4) + "," + String(gy, 4) + "," + String(gz, 4);
  Serial.print(payload);
  Serial.print("*");
  char crcText[5];
  snprintf(crcText, sizeof(crcText), "%04X", crc16(payload));
  Serial.println(crcText);
}

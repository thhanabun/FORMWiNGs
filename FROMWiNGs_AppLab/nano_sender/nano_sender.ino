#include "Arduino_LSM9DS1.h"

/*
  FormSense Nano filtered CSV sender for Arduino Nano 33 BLE Sense.

  Wiring for pin UART:
    Nano TX/D1 -> UNO Q RX
    Nano GND   -> UNO Q GND

  Output on Serial1:
    seq,timestamp_s,acc_x_g,acc_y_g,acc_z_g,gyro_x_dps,gyro_y_dps,gyro_z_dps

  USB Serial is kept for human-readable status only.
*/

constexpr unsigned long USB_BAUD_RATE = 115200;
constexpr unsigned long UART_BAUD_RATE = 115200;
constexpr unsigned long PERIOD_US = 20000;  // 50 Hz
constexpr float FILTER_CUTOFF_HZ = 35.0f;

uint32_t sequenceNumber = 0;
uint32_t lastSampleUs = 0;
uint32_t lastStatusMs = 0;
bool filterReady = false;
bool haveAccSample = false;
bool haveGyroSample = false;

float accLatest[3] = {0.0f, 0.0f, 1.0f};
float gyroLatest[3] = {0.0f, 0.0f, 0.0f};
float accFiltered[3] = {0.0f, 0.0f, 1.0f};
float gyroFiltered[3] = {0.0f, 0.0f, 0.0f};

float lowPass(float previous, float current, float dt) {
  const float rc = 1.0f / (2.0f * PI * FILTER_CUTOFF_HZ);
  const float alpha = dt / (rc + dt);
  return previous + alpha * (current - previous);
}

void updateFilters(float ax, float ay, float az, float gx, float gy, float gz, float dt) {
  if (!filterReady) {
    accFiltered[0] = ax;
    accFiltered[1] = ay;
    accFiltered[2] = az;
    gyroFiltered[0] = gx;
    gyroFiltered[1] = gy;
    gyroFiltered[2] = gz;
    filterReady = true;
    return;
  }

  accFiltered[0] = lowPass(accFiltered[0], ax, dt);
  accFiltered[1] = lowPass(accFiltered[1], ay, dt);
  accFiltered[2] = lowPass(accFiltered[2], az, dt);
  gyroFiltered[0] = lowPass(gyroFiltered[0], gx, dt);
  gyroFiltered[1] = lowPass(gyroFiltered[1], gy, dt);
  gyroFiltered[2] = lowPass(gyroFiltered[2], gz, dt);
}

void sendCsv(float timestampS) {
  Serial1.print(sequenceNumber++);
  Serial1.print(',');
  Serial1.print(timestampS, 4);
  Serial1.print(',');
  Serial1.print(accFiltered[0], 5);
  Serial1.print(',');
  Serial1.print(accFiltered[1], 5);
  Serial1.print(',');
  Serial1.print(accFiltered[2], 5);
  Serial1.print(',');
  Serial1.print(gyroFiltered[0], 4);
  Serial1.print(',');
  Serial1.print(gyroFiltered[1], 4);
  Serial1.print(',');
  Serial1.println(gyroFiltered[2], 4);
}

void setup() {
  Serial.begin(USB_BAUD_RATE);
  Serial1.begin(UART_BAUD_RATE);

  while (!Serial && millis() < 2500) {
    ;
  }

  if (!IMU.begin()) {
    Serial.println("ERROR: IMU_INIT_FAILED");
    while (true) {
      delay(1000);
    }
  }

  lastSampleUs = micros();
  Serial.println("Nano FormSense filtered CSV sender ready");
  Serial.print("UART baud: ");
  Serial.println(UART_BAUD_RATE);
  Serial.print("Target sample rate Hz: ");
  Serial.println(1000000UL / PERIOD_US);
}

void loop() {
  const uint32_t nowUs = micros();

  if (IMU.accelerationAvailable()) {
    IMU.readAcceleration(accLatest[0], accLatest[1], accLatest[2]);
    haveAccSample = true;
  }

  if (IMU.gyroscopeAvailable()) {
    IMU.readGyroscope(gyroLatest[0], gyroLatest[1], gyroLatest[2]);
    haveGyroSample = true;
  }

  const uint32_t elapsedUs = nowUs - lastSampleUs;
  if (elapsedUs < PERIOD_US) {
    if (millis() - lastStatusMs >= 1000) {
      lastStatusMs = millis();
      Serial.print("CSV packets sent: ");
      Serial.print(sequenceNumber);
      Serial.print(" acc=");
      Serial.print(haveAccSample ? "ok" : "wait");
      Serial.print(" gyro=");
      Serial.println(haveGyroSample ? "ok" : "wait");
    }
    return;
  }

  if (!haveAccSample || !haveGyroSample) {
    return;
  }

  lastSampleUs = nowUs;

  updateFilters(
    accLatest[0],
    accLatest[1],
    accLatest[2],
    gyroLatest[0],
    gyroLatest[1],
    gyroLatest[2],
    elapsedUs / 1000000.0f
  );
  sendCsv(nowUs / 1000000.0f);

  if (millis() - lastStatusMs >= 1000) {
    lastStatusMs = millis();
    Serial.print("CSV packets sent: ");
    Serial.println(sequenceNumber);
  }
}

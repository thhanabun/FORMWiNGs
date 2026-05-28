#include <Arduino.h>
#include <ArduinoBLE.h>
#include <Arduino_RouterBridge.h>
#include <Wire.h>

/*
  Full FormSense bridge:

    Nano TX/D1 -> UNO Q RX/D0 MCU
    UNO Q MCU  -> RouterBridge -> UNO Q Linux Python model
  UNO Q Linux -> RouterBridge -> UNO Q MCU BLE notify

  Nano CSV on Serial1:
    seq,timestamp_s,acc_x_g,acc_y_g,acc_z_g,gyro_x_dps,gyro_y_dps,gyro_z_dps

  Linux side:
    python/uno_q_bridge_live_inference.py
*/

const uint32_t UART_BAUD_RATE = 115200;
const uint32_t STATUS_INTERVAL_MS = 1000;
const uint32_t BRIDGE_FLUSH_INTERVAL_MS = 120;
const uint32_t THERMAL_INTERVAL_MS = 2000;
const uint32_t THERMAL_CONVERSION_MS = 40;
const uint32_t BLE_NOTIFY_INTERVAL_MS = 40;
const uint8_t THERMAL_I2C_ADDRESS = 0x44;
const size_t RX_BUFFER_SIZE = 128;
const size_t BRIDGE_BATCH_SIZE = 512;
const size_t BRIDGE_QUEUE_SIZE = 32;
const size_t BLE_CHARACTERISTIC_SIZE = 64;
const size_t BLE_VALUE_SIZE = BLE_CHARACTERISTIC_SIZE + 1;
const size_t BLE_NOTIFY_QUEUE_SIZE = 128;

const char *SERVICE_UUID = "19B10000-E8F2-537E-4F6C-D104768A1214";
const char *CHARACTERISTIC_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214";

BLEService formSenseService(SERVICE_UUID);
BLEStringCharacteristic formSenseCharacteristic(
  CHARACTERISTIC_UUID,
  BLERead | BLENotify,
  BLE_CHARACTERISTIC_SIZE
);

char rxBuffer[RX_BUFFER_SIZE];
char bridgeBatch[BRIDGE_BATCH_SIZE];
char pendingBridgeBatches[BRIDGE_QUEUE_SIZE][BRIDGE_BATCH_SIZE];
char pendingBlePackets[BLE_NOTIFY_QUEUE_SIZE][BLE_VALUE_SIZE];
char latestSensorLine[RX_BUFFER_SIZE] = "";
char latestThermalLine[64] = "";
char pendingBle[BLE_VALUE_SIZE] = "";
char latestBle[BLE_VALUE_SIZE] = "";
size_t rxIndex = 0;
size_t batchIndex = 0;
size_t pendingBleIndex = 0;
uint8_t bridgeQueueHead = 0;
uint8_t bridgeQueueTail = 0;
uint8_t bridgeQueueCount = 0;
uint8_t bleQueueHead = 0;
uint8_t bleQueueTail = 0;
uint8_t bleQueueCount = 0;

bool pendingBleChunkOk = true;
bool bleClientConnected = false;
bool lastBleClientConnected = false;
uint32_t uartPackets = 0;
uint32_t bridgeBatches = 0;
uint32_t bridgePopCalls = 0;
uint32_t bridgePopHits = 0;
uint32_t droppedFrames = 0;
uint32_t droppedBridge = 0;
uint32_t droppedBle = 0;
uint32_t bleUpdates = 0;
uint32_t lastBleUpdates = 0;
uint32_t lastBleNotifyMs = 0;
uint32_t lastPacketMs = 0;
uint32_t lastFlushMs = 0;
uint32_t lastStatusMs = 0;
uint32_t lastThermalRequestMs = 0;
uint32_t thermalReadyMs = 0;
uint32_t thermalReads = 0;
uint32_t thermalErrors = 0;
bool thermalPending = false;
bool hasThermalLine = false;
TwoWire *thermalWire = &Wire1;
const char *thermalWireName = "Wire1";

void clearRuntimeQueues() {
  while (Serial1.available() > 0) {
    Serial1.read();
  }
  rxIndex = 0;
  batchIndex = 0;
  bridgeBatch[0] = '\0';
  bridgeQueueHead = 0;
  bridgeQueueTail = 0;
  bridgeQueueCount = 0;
  pendingBle[0] = '\0';
  pendingBleIndex = 0;
  pendingBleChunkOk = true;
  lastPacketMs = millis();
}

void clearPendingBlePayload() {
  pendingBle[0] = '\0';
  pendingBleIndex = 0;
  pendingBleChunkOk = true;
  bleQueueHead = 0;
  bleQueueTail = 0;
  bleQueueCount = 0;
}

String bleConnectedPayload() {
  return bleClientConnected ? "1" : "0";
}

bool beginBlePayload(String payload) {
  (void)payload;
  pendingBle[0] = '\0';
  pendingBleIndex = 0;
  pendingBleChunkOk = true;
  return true;
}

bool receiveBleChunk(String payload) {
  if (!pendingBleChunkOk) {
    return false;
  }
  const size_t length = payload.length();
  if (pendingBleIndex + length >= BLE_VALUE_SIZE) {
    pendingBleChunkOk = false;
    return false;
  }
  for (size_t index = 0; index < length; index++) {
    pendingBle[pendingBleIndex++] = payload.charAt(index);
  }
  pendingBle[pendingBleIndex] = '\0';
  return true;
}

bool commitBlePayload(String payload) {
  (void)payload;
  if (!pendingBleChunkOk || pendingBleIndex == 0) {
    pendingBle[0] = '\0';
    pendingBleIndex = 0;
    pendingBleChunkOk = true;
    return false;
  }

  if (bleQueueCount >= BLE_NOTIFY_QUEUE_SIZE) {
    droppedBle++;
    pendingBle[0] = '\0';
    pendingBleIndex = 0;
    pendingBleChunkOk = true;
    return false;
  }

  strncpy(pendingBlePackets[bleQueueTail], pendingBle, BLE_VALUE_SIZE - 1);
  pendingBlePackets[bleQueueTail][BLE_VALUE_SIZE - 1] = '\0';
  bleQueueTail = (bleQueueTail + 1) % BLE_NOTIFY_QUEUE_SIZE;
  bleQueueCount++;
  pendingBle[0] = '\0';
  pendingBleIndex = 0;
  pendingBleChunkOk = true;
  return true;
}

String popThermalPayload() {
  if (!hasThermalLine) {
    return "";
  }
  return String(latestThermalLine);
}

String popBridgeBatch() {
  bridgePopCalls++;
  if (bridgeQueueCount == 0) {
    return "";
  }

  String payload(pendingBridgeBatches[bridgeQueueHead]);
  pendingBridgeBatches[bridgeQueueHead][0] = '\0';
  bridgeQueueHead = (bridgeQueueHead + 1) % BRIDGE_QUEUE_SIZE;
  bridgeQueueCount--;
  bridgePopHits++;
  return payload;
}

bool looksLikeFormsenseCsv(const char *line) {
  int commas = 0;
  for (const char *cursor = line; *cursor != '\0'; ++cursor) {
    if (*cursor == ',') {
      commas++;
    }
  }
  return commas == 7;
}

void flushBridgeBatch() {
  if (batchIndex == 0) {
    return;
  }

  bridgeBatch[batchIndex] = '\0';

  if (bridgeQueueCount >= BRIDGE_QUEUE_SIZE) {
    bridgeQueueHead = (bridgeQueueHead + 1) % BRIDGE_QUEUE_SIZE;
    bridgeQueueCount--;
    droppedBridge++;
  }

  strncpy(pendingBridgeBatches[bridgeQueueTail], bridgeBatch, BRIDGE_BATCH_SIZE - 1);
  pendingBridgeBatches[bridgeQueueTail][BRIDGE_BATCH_SIZE - 1] = '\0';
  bridgeQueueTail = (bridgeQueueTail + 1) % BRIDGE_QUEUE_SIZE;
  bridgeQueueCount++;
  bridgeBatches++;

  batchIndex = 0;
  bridgeBatch[0] = '\0';
  lastFlushMs = millis();
}

void appendToBridgeBatch(const char *line) {
  const size_t lineLength = strlen(line);
  if (lineLength + 2 >= BRIDGE_BATCH_SIZE) {
    droppedBridge++;
    return;
  }

  if (batchIndex > 0 && batchIndex + lineLength + 2 >= BRIDGE_BATCH_SIZE) {
    flushBridgeBatch();
  }

  if (batchIndex + lineLength + 2 >= BRIDGE_BATCH_SIZE) {
    droppedBridge++;
    return;
  }

  memcpy(bridgeBatch + batchIndex, line, lineLength);
  batchIndex += lineLength;
  bridgeBatch[batchIndex++] = '\n';
  bridgeBatch[batchIndex] = '\0';
}

void handleLine(char *line) {
  if (!looksLikeFormsenseCsv(line)) {
    droppedFrames++;
    return;
  }

  uartPackets++;
  lastPacketMs = millis();
  strncpy(latestSensorLine, line, RX_BUFFER_SIZE - 1);
  latestSensorLine[RX_BUFFER_SIZE - 1] = '\0';
  appendToBridgeBatch(line);
}

void readUart() {
  while (Serial1.available() > 0) {
    const char c = static_cast<char>(Serial1.read());

    if (c == '\r') {
      continue;
    }

    if (c == '\n') {
      rxBuffer[rxIndex] = '\0';
      if (rxIndex > 0) {
        handleLine(rxBuffer);
      }
      rxIndex = 0;
      continue;
    }

    if (rxIndex < RX_BUFFER_SIZE - 1) {
      rxBuffer[rxIndex++] = c;
    } else {
      rxIndex = 0;
      droppedFrames++;
    }
  }
}

void serviceBridgeFlush() {
  if (batchIndex > 0 && millis() - lastFlushMs >= BRIDGE_FLUSH_INTERVAL_MS) {
    flushBridgeBatch();
  }
}

bool i2cAddressResponds(TwoWire &bus, uint8_t address) {
  bus.beginTransmission(address);
  return bus.endTransmission() == 0;
}

void selectThermalBus() {
  if (i2cAddressResponds(Wire1, THERMAL_I2C_ADDRESS)) {
    thermalWire = &Wire1;
    thermalWireName = "Wire1";
  } else if (i2cAddressResponds(Wire, THERMAL_I2C_ADDRESS)) {
    thermalWire = &Wire;
    thermalWireName = "Wire";
  }

  Monitor.print("Thermal bus selected: ");
  Monitor.print(thermalWireName);
  Monitor.print(" address=0x");
  Monitor.println(THERMAL_I2C_ADDRESS, HEX);
}

void serviceThermal() {
  const uint32_t now = millis();

  if (!thermalPending && now - lastThermalRequestMs >= THERMAL_INTERVAL_MS) {
    thermalWire->beginTransmission(THERMAL_I2C_ADDRESS);
    const uint8_t result = thermalWire->endTransmission();
    lastThermalRequestMs = now;
    if (result == 0) {
      thermalPending = true;
      thermalReadyMs = now + THERMAL_CONVERSION_MS;
    } else {
      thermalErrors++;
    }
  }

  if (!thermalPending || static_cast<int32_t>(now - thermalReadyMs) < 0) {
    return;
  }

  thermalPending = false;
  const int count = thermalWire->requestFrom(static_cast<int>(THERMAL_I2C_ADDRESS), 4);
  if (count != 4) {
    thermalErrors++;
    while (thermalWire->available() > 0) {
      thermalWire->read();
    }
    return;
  }

  const uint8_t data0 = static_cast<uint8_t>(thermalWire->read());
  const uint8_t data1 = static_cast<uint8_t>(thermalWire->read());
  const uint8_t data2 = static_cast<uint8_t>(thermalWire->read());
  const uint8_t data3 = static_cast<uint8_t>(thermalWire->read());
  const uint8_t status = data0 >> 6;
  if (status == 3) {
    thermalErrors++;
    return;
  }

  const uint16_t rawHumidity = (static_cast<uint16_t>(data0 & 0x3F) << 8) | data1;
  const uint16_t rawTemperature = (static_cast<uint16_t>(data2) << 6) | (data3 >> 2);
  const float humidityPct = (static_cast<float>(rawHumidity) * 100.0f) / 16383.0f;
  const float temperatureC = (static_cast<float>(rawTemperature) * 165.0f) / 16383.0f - 40.0f;

  snprintf(latestThermalLine, sizeof(latestThermalLine), "%lu,%.2f,%.2f",
           static_cast<unsigned long>(now), temperatureC, humidityPct);
  hasThermalLine = true;
  thermalReads++;
}

void serviceBle() {
  if (!bleClientConnected || bleQueueCount == 0) {
    return;
  }

  if (millis() - lastBleNotifyMs < BLE_NOTIFY_INTERVAL_MS) {
    return;
  }
  lastBleNotifyMs = millis();

  strncpy(latestBle, pendingBlePackets[bleQueueHead], BLE_VALUE_SIZE - 1);
  latestBle[BLE_VALUE_SIZE - 1] = '\0';
  pendingBlePackets[bleQueueHead][0] = '\0';
  bleQueueHead = (bleQueueHead + 1) % BLE_NOTIFY_QUEUE_SIZE;
  bleQueueCount--;
  formSenseCharacteristic.writeValue(latestBle);
  bleUpdates++;
}

void serviceBleConnection() {
  BLE.poll();
  bleClientConnected = BLE.connected();
  if (bleClientConnected == lastBleClientConnected) {
    return;
  }

  lastBleClientConnected = bleClientConnected;
  if (bleClientConnected) {
    lastThermalRequestMs = millis() - THERMAL_INTERVAL_MS;
    Monitor.println("BLE client connected; model/BLE output active");
  } else {
    clearPendingBlePayload();
    Monitor.println("BLE client disconnected; collecting local sensor data");
  }
}

void printStatus() {
  if (millis() - lastStatusMs < STATUS_INTERVAL_MS) {
    return;
  }
  lastStatusMs = millis();
  const uint32_t bleDelta = bleUpdates - lastBleUpdates;
  lastBleUpdates = bleUpdates;

  Monitor.print("uart_packets=");
  Monitor.print(uartPackets);
  Monitor.print(" bridge_batches=");
  Monitor.print(bridgeBatches);
  Monitor.print(" bridge_pop_calls=");
  Monitor.print(bridgePopCalls);
  Monitor.print(" bridge_pop_hits=");
  Monitor.print(bridgePopHits);
  Monitor.print(" bridge_q=");
  Monitor.print(bridgeQueueCount);
  Monitor.print(" ble_connected=");
  Monitor.print(bleClientConnected ? 1 : 0);
  Monitor.print(" thermal_reads=");
  Monitor.print(thermalReads);
  Monitor.print(" thermal_errors=");
  Monitor.print(thermalErrors);
  Monitor.print(" ble_updates=");
  Monitor.print(bleUpdates);
  Monitor.print(" ble_hz=");
  Monitor.print(bleDelta);
  Monitor.print(" ble_q=");
  Monitor.print(bleQueueCount);
  Monitor.print(" dropped_frames=");
  Monitor.print(droppedFrames);
  Monitor.print(" dropped_bridge=");
  Monitor.print(droppedBridge);
  Monitor.print(" dropped_ble=");
  Monitor.print(droppedBle);
  if (latestSensorLine[0] != '\0') {
    Monitor.print(" latest_sensor=");
    Monitor.print(latestSensorLine);
  }
  if (latestThermalLine[0] != '\0') {
    Monitor.print(" latest_thermal=");
    Monitor.print(latestThermalLine);
  }
  if (latestBle[0] != '\0') {
    Monitor.print(" latest_model_bytes=");
    Monitor.println(strlen(latestBle));
  } else {
    Monitor.println(" latest_model=<waiting_for_model_output>");
  }

  if (bleClientConnected && millis() - lastPacketMs > 3000) {
    Monitor.println("WARN: no UART packet from Nano for 3s");
  }
}

void setup() {
  Serial1.begin(UART_BAUD_RATE);
  Wire.begin();
  Wire1.begin();

  Bridge.begin();
  Monitor.begin(115200);
  while (!Monitor) {
    delay(100);
  }

  if (!BLE.begin()) {
    Monitor.println("Starting BLE failed!");
    while (true) {
      delay(1000);
    }
  }

  BLE.setLocalName("FROMWiNGs");
  BLE.setDeviceName("FROMWiNGs");
  BLE.setAdvertisedService(formSenseService);
  formSenseService.addCharacteristic(formSenseCharacteristic);
  BLE.addService(formSenseService);
  BLE.advertise();

  Bridge.provide_safe("formsense/ble_begin", beginBlePayload);
  Bridge.provide_safe("formsense/ble_chunk", receiveBleChunk);
  Bridge.provide_safe("formsense/ble_commit", commitBlePayload);
  Bridge.provide_safe("formsense/ble_connected", bleConnectedPayload);
  Bridge.provide_safe("formsense/pop_imu_batch", popBridgeBatch);
  Bridge.provide_safe("formsense/pop_thermal", popThermalPayload);

  lastPacketMs = millis();
  lastFlushMs = millis();
  lastThermalRequestMs = millis() - THERMAL_INTERVAL_MS;
  Monitor.println("UNO Q UART -> Linux model -> MCU BLE bridge ready");
  Monitor.print("UART baud: ");
  Monitor.println(UART_BAUD_RATE);
  Monitor.println("BLE name: FROMWiNGs");
  Monitor.println("Thermal I2C: Modulino Thermo HS300x on address 0x44");
  selectThermalBus();
}

void loop() {
  serviceBleConnection();
  readUart();
  serviceBridgeFlush();
  serviceThermal();
  serviceBle();
  printStatus();
  delay(0);
}

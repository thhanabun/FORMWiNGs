#include <Arduino.h>
#include <ArduinoBLE.h>
#include <Arduino_RouterBridge.h>

/*
  Full FormSense bridge:

    Nano TX/D1 -> UNO Q RX/D0 MCU
    UNO Q MCU  -> RouterBridge -> UNO Q Linux Python model
  UNO Q Linux -> RouterBridge -> UNO Q MCU BLE notify

  Nano CSV on Serial1:
    seq,timestamp_s,acc_x_g,acc_y_g,acc_z_g,gyro_x_dps,gyro_y_dps,gyro_z_dps

  Linux side:
    python/uno_q_bridge_live_inference.py --mcu-ble
*/

const uint32_t UART_BAUD_RATE = 115200;
const uint32_t STATUS_INTERVAL_MS = 1000;
const uint32_t BRIDGE_FLUSH_INTERVAL_MS = 80;
const size_t RX_BUFFER_SIZE = 128;
const size_t BRIDGE_BATCH_SIZE = 512;
const size_t BRIDGE_QUEUE_SIZE = 8;
const size_t BLE_VALUE_SIZE = 1200;

const char *SERVICE_UUID = "19B10000-E8F2-537E-4F6C-D104768A1214";
const char *CHARACTERISTIC_UUID = "19B10001-E8F2-537E-4F6C-D104768A1214";

BLEService formSenseService(SERVICE_UUID);
BLEStringCharacteristic formSenseCharacteristic(
  CHARACTERISTIC_UUID,
  BLERead | BLENotify,
  BLE_VALUE_SIZE
);

char rxBuffer[RX_BUFFER_SIZE];
char bridgeBatch[BRIDGE_BATCH_SIZE];
char pendingBridgeBatches[BRIDGE_QUEUE_SIZE][BRIDGE_BATCH_SIZE];
char latestSensorLine[RX_BUFFER_SIZE] = "";
char pendingBle[BLE_VALUE_SIZE] = "";
char latestBle[BLE_VALUE_SIZE] = "";
size_t rxIndex = 0;
size_t batchIndex = 0;
size_t pendingBleIndex = 0;
uint8_t bridgeQueueHead = 0;
uint8_t bridgeQueueTail = 0;
uint8_t bridgeQueueCount = 0;

volatile bool hasPendingBle = false;
bool pendingBleChunkOk = true;
uint32_t uartPackets = 0;
uint32_t bridgeBatches = 0;
uint32_t bridgePopCalls = 0;
uint32_t bridgePopHits = 0;
uint32_t droppedFrames = 0;
uint32_t droppedBridge = 0;
uint32_t bleUpdates = 0;
uint32_t lastBleUpdates = 0;
uint32_t lastPacketMs = 0;
uint32_t lastFlushMs = 0;
uint32_t lastStatusMs = 0;

bool receiveBlePayload(String payload) {
  payload.trim();
  if (payload.length() == 0) {
    return false;
  }
  payload.toCharArray(pendingBle, BLE_VALUE_SIZE);
  hasPendingBle = true;
  return true;
}

bool beginBlePayload(String payload) {
  (void)payload;
  pendingBle[0] = '\0';
  pendingBleIndex = 0;
  pendingBleChunkOk = true;
  hasPendingBle = false;
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
  hasPendingBle = true;
  return true;
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

void serviceBle() {
  BLE.poll();
  if (!hasPendingBle) {
    return;
  }

  strncpy(latestBle, pendingBle, BLE_VALUE_SIZE - 1);
  latestBle[BLE_VALUE_SIZE - 1] = '\0';
  hasPendingBle = false;
  formSenseCharacteristic.writeValue(latestBle);
  bleUpdates++;
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
  Monitor.print(" ble_updates=");
  Monitor.print(bleUpdates);
  Monitor.print(" ble_hz=");
  Monitor.print(bleDelta);
  Monitor.print(" dropped_frames=");
  Monitor.print(droppedFrames);
  Monitor.print(" dropped_bridge=");
  Monitor.print(droppedBridge);
  if (latestSensorLine[0] != '\0') {
    Monitor.print(" latest_sensor=");
    Monitor.print(latestSensorLine);
  }
  if (latestBle[0] != '\0') {
    Monitor.print(" latest_model=");
    Monitor.println(latestBle);
  } else {
    Monitor.println(" latest_model=<waiting_for_model_output>");
  }

  if (millis() - lastPacketMs > 3000) {
    Monitor.println("WARN: no UART packet from Nano for 3s");
  }
}

void setup() {
  Serial1.begin(UART_BAUD_RATE);

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

  Bridge.provide_safe("formsense/ble_notify", receiveBlePayload);
  Bridge.provide_safe("formsense/ble_begin", beginBlePayload);
  Bridge.provide_safe("formsense/ble_chunk", receiveBleChunk);
  Bridge.provide_safe("formsense/ble_commit", commitBlePayload);
  Bridge.provide_safe("formsense/pop_imu_batch", popBridgeBatch);

  lastPacketMs = millis();
  lastFlushMs = millis();
  Monitor.println("UNO Q UART -> Linux model -> MCU BLE bridge ready");
  Monitor.print("UART baud: ");
  Monitor.println(UART_BAUD_RATE);
  Monitor.println("BLE name: FROMWiNGs");
}

void loop() {
  readUart();
  serviceBridgeFlush();
  serviceBle();
  printStatus();
  delay(0);
}

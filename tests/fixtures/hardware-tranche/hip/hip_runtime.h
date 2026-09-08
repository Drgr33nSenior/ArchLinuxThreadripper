#ifndef WORKSTATION_TEST_MOCK_HIP_RUNTIME_H
#define WORKSTATION_TEST_MOCK_HIP_RUNTIME_H

// Bounded test double for hip-peer-copy.cpp. It exercises control flow only:
// it neither links ROCm nor discovers, allocates on, or executes a real GPU.

#include <cstdio>
#include <cstdlib>
#include <cstring>

enum hipError_t {
  hipSuccess = 0,
  hipErrorInvalidValue,
  hipErrorPeerAccessAlreadyEnabled,
  hipErrorUnknown,
};

struct hipUUID {
  unsigned char bytes[16];
};

enum hipMemcpyKind {
  hipMemcpyHostToDevice,
  hipMemcpyDeviceToHost,
};

namespace workstation_mock_hip {

inline const char* mode() {
  const char* value = std::getenv("MOCK_HIP_MODE");
  return value == nullptr ? "success" : value;
}

inline bool mode_is(const char* expected) {
  return std::strcmp(mode(), expected) == 0;
}

inline bool valid_device(int device) {
  return device == 0 || device == 1;
}

inline int& current_device() {
  static int device = 0;
  return device;
}

}  // namespace workstation_mock_hip

inline const char* hipGetErrorString(hipError_t error) {
  switch (error) {
    case hipSuccess: return "hipSuccess";
    case hipErrorInvalidValue: return "hipErrorInvalidValue";
    case hipErrorPeerAccessAlreadyEnabled: return "hipErrorPeerAccessAlreadyEnabled";
    default: return "hipErrorUnknown";
  }
}

inline hipError_t hipGetDeviceCount(int* count) {
  if (count == nullptr) {
    return hipErrorInvalidValue;
  }
  *count = 2;
  return hipSuccess;
}

inline hipError_t hipDeviceGetPCIBusId(char* bus_id, int length, int device) {
  if (bus_id == nullptr || length < 13 || !workstation_mock_hip::valid_device(device)) {
    return hipErrorInvalidValue;
  }
  std::snprintf(bus_id, static_cast<std::size_t>(length), "0000:0%d:00.0", device + 1);
  return hipSuccess;
}

inline hipError_t hipDeviceGetUuid(hipUUID* uuid, int device) {
  if (uuid == nullptr || !workstation_mock_hip::valid_device(device)) {
    return hipErrorInvalidValue;
  }
  for (unsigned char index = 0; index < 16; ++index) {
    uuid->bytes[index] = static_cast<unsigned char>((device * 16) + index);
  }
  return hipSuccess;
}

inline hipError_t hipDeviceCanAccessPeer(int* can_access, int device, int peer_device) {
  if (can_access == nullptr || !workstation_mock_hip::valid_device(device) ||
      !workstation_mock_hip::valid_device(peer_device)) {
    return hipErrorInvalidValue;
  }
  *can_access = workstation_mock_hip::mode_is("no-capability") ? 0 : 1;
  return hipSuccess;
}

inline hipError_t hipSetDevice(int device) {
  if (!workstation_mock_hip::valid_device(device)) {
    return hipErrorInvalidValue;
  }
  workstation_mock_hip::current_device() = device;
  return hipSuccess;
}

inline hipError_t hipDeviceEnablePeerAccess(int peer_device, unsigned int flags) {
  if (!workstation_mock_hip::valid_device(peer_device) || flags != 0) {
    return hipErrorInvalidValue;
  }
  return hipSuccess;
}

inline hipError_t hipDeviceDisablePeerAccess(int peer_device) {
  return workstation_mock_hip::valid_device(peer_device) ? hipSuccess : hipErrorInvalidValue;
}

inline hipError_t hipMalloc(void** pointer, std::size_t bytes) {
  if (pointer == nullptr || bytes == 0) {
    return hipErrorInvalidValue;
  }
  *pointer = std::malloc(bytes);
  return *pointer == nullptr ? hipErrorUnknown : hipSuccess;
}

inline hipError_t hipFree(void* pointer) {
  std::free(pointer);
  return hipSuccess;
}

inline hipError_t hipMemcpy(void* destination, const void* source, std::size_t bytes,
                            hipMemcpyKind kind) {
  if (destination == nullptr || source == nullptr) {
    return hipErrorInvalidValue;
  }
  std::memcpy(destination, source, bytes);
  if (kind == hipMemcpyDeviceToHost &&
      workstation_mock_hip::mode_is("corrupt-readback") && bytes != 0) {
    static_cast<unsigned char*>(destination)[0] ^= 0xffU;
  }
  return hipSuccess;
}

inline hipError_t hipMemcpyPeer(void* destination, int destination_device,
                                const void* source, int source_device,
                                std::size_t bytes) {
  if (destination == nullptr || source == nullptr ||
      !workstation_mock_hip::valid_device(destination_device) ||
      !workstation_mock_hip::valid_device(source_device)) {
    return hipErrorInvalidValue;
  }
  if (workstation_mock_hip::mode_is("copy-failure")) {
    return hipErrorUnknown;
  }
  std::memcpy(destination, source, bytes);
  return hipSuccess;
}

inline hipError_t hipDeviceSynchronize() {
  return hipSuccess;
}

#endif

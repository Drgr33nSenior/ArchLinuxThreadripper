#pragma once
// Deliberate API/control-flow simulation. Does NOT implement real HIP IPC.
#include <cstdio>
#include <cstdlib>
#include <cstring>
using hipError_t = int;
constexpr hipError_t hipSuccess = 0;
constexpr int hipMemcpyHostToDevice = 1, hipMemcpyDeviceToHost = 2;
constexpr unsigned hipIpcMemLazyEnablePeerAccess = 1;
struct hipIpcMemHandle_t { char data[64]; };
inline const char* hipGetErrorString(int) { return "simulated HIP failure"; }
inline int hipGetDeviceCount(int* count) { *count = 2; return 0; }
inline int hipSetDevice(int device) { return device < 0 || device > 1; }
inline int hipMalloc(void** memory, size_t size) { *memory = std::malloc(size); return *memory ? 0 : 1; }
inline int hipFree(void* memory) { std::free(memory); return 0; }
inline int hipMemcpy(void* dst, const void* src, size_t size, int direction) {
  std::memcpy(dst, src, size);
  if (direction == hipMemcpyDeviceToHost && std::getenv("MOCK_IPC_CORRUPT")) static_cast<char*>(dst)[0] ^= 1;
  return 0;
}
inline int hipDeviceSynchronize() { return 0; }
inline int hipIpcGetMemHandle(hipIpcMemHandle_t* handle, void*) {
  std::memset(handle, 0, sizeof(*handle)); handle->data[0] = 42;
  return std::getenv("MOCK_IPC_EXPORT_FAIL") ? 1 : 0;
}
inline int hipIpcOpenMemHandle(void** memory, hipIpcMemHandle_t handle, unsigned) {
  if (std::getenv("MOCK_IPC_IMPORT_FAIL") || handle.data[0] != 42) return 1;
  const size_t size = 16U * 1024U * 1024U;
  *memory = std::malloc(size);
  if (!*memory) return 1;
  // Independent simulated allocation: correctness here tests rejection/control
  // flow only, never memory sharing, physical transport or GPU correctness.
  for (size_t i=0; i<size; ++i) static_cast<unsigned char*>(*memory)[i] = i % 251;
  return 0;
}
inline int hipIpcCloseMemHandle(void* memory) { std::free(memory); return 0; }
inline int hipDeviceGetPCIBusId(char* pci, int length, int device) { std::snprintf(pci, length, "0000:%02x:00.0", device+1); return 0; }

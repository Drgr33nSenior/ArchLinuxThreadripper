#include <hip/hip_runtime.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#define HIP_CHECK(call) do { hipError_t e = (call); if (e != hipSuccess) { \
  std::fprintf(stderr, "%s: %s\n", #call, hipGetErrorString(e)); return 1; } } while (0)

__global__ void write_value(int* result, int value) { *result = value; }

int main(int argc, char** argv) {
  if (argc != 4) return 2;
  int count = 0;
  HIP_CHECK(hipGetDeviceCount(&count));
  if (count != std::atoi(argv[1])) return 3;
  char previous[32] = {};
  for (int i = 0; i < count; ++i) {
    hipDeviceProp_t p{};
    char bdf[32] = {};
    HIP_CHECK(hipGetDeviceProperties(&p, i));
    HIP_CHECK(hipDeviceGetPCIBusId(bdf, sizeof(bdf), i));
    if (i && std::strcmp(previous, bdf) == 0) return 4;
    std::strcpy(previous, bdf);
    const std::size_t n = std::strlen(argv[2]);
    if (std::strncmp(p.gcnArchName, argv[2], n) != 0 ||
        (p.gcnArchName[n] != '\0' && p.gcnArchName[n] != ':') ||
        !std::strstr(p.name, argv[3])) return 5;
    HIP_CHECK(hipSetDevice(i));
    int* device = nullptr;
    int result = 0;
    HIP_CHECK(hipMalloc(&device, sizeof(int)));
    hipLaunchKernelGGL(write_value, dim3(1), dim3(1), 0, 0, device, i + 42);
    HIP_CHECK(hipGetLastError());
    HIP_CHECK(hipDeviceSynchronize());
    HIP_CHECK(hipMemcpy(&result, device, sizeof(int), hipMemcpyDeviceToHost));
    HIP_CHECK(hipFree(device));
    if (result != i + 42) return 6;
    std::printf("PASS device=%d pci=%s name=%s arch=%s vram=%zu\n", i, bdf, p.name, p.gcnArchName, p.totalGlobalMem);
  }
  return 0;
}

// Target-only HIP IPC correctness. Child execs a fresh process (never uses a
// fork-inherited HIP runtime). Exporter retains allocation until importer exits.
#include <hip/hip_runtime.h>
#include <sys/wait.h>
#include <unistd.h>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

constexpr size_t bytes = 16U * 1024U * 1024U;
bool check(hipError_t result) {
  if (result == hipSuccess) return true;
  std::fprintf(stderr, "HIP IPC error: %s\n", hipGetErrorString(result));
  return false;
}
bool transfer(int fd, void* memory, size_t length, bool writing) {
  auto* cursor = static_cast<char*>(memory);
  while (length) {
    ssize_t n = writing ? write(fd, cursor, length) : read(fd, cursor, length);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) return false;
    cursor += n; length -= static_cast<size_t>(n);
  }
  return true;
}
int main(int argc, char** argv) {
  if (argc == 4 && std::strcmp(argv[1], "--import") == 0) {
    hipIpcMemHandle_t handle{};
    const int fd = std::atoi(argv[2]);
    const int device = std::atoi(argv[3]);
    if (!transfer(fd, &handle, sizeof(handle), false)) return 1;
    close(fd);
    void* memory = nullptr;
    if (!check(hipSetDevice(device)) || !check(hipIpcOpenMemHandle(&memory, handle, hipIpcMemLazyEnablePeerAccess))) return 1;
    std::vector<unsigned char> actual(bytes);
    bool ok = check(hipMemcpy(actual.data(), memory, bytes, hipMemcpyDeviceToHost));
    for (size_t i = 0; ok && i < bytes; ++i) ok = actual[i] == static_cast<unsigned char>(i % 251);
    ok = check(hipIpcCloseMemHandle(memory)) && ok;
    return ok ? 0 : 1;
  }
  if (argc != 1) return 2;
  int count = 0;
  if (!check(hipGetDeviceCount(&count)) || count != 2) return 1;
  for (int source = 0; source < count; ++source) {
    void* memory = nullptr;
    hipIpcMemHandle_t handle{};
    std::vector<unsigned char> expected(bytes);
    for (size_t i = 0; i < bytes; ++i) expected[i] = static_cast<unsigned char>(i % 251);
    if (!check(hipSetDevice(source)) || !check(hipMalloc(&memory, bytes))) return 1;
    bool ok = check(hipMemcpy(memory, expected.data(), bytes, hipMemcpyHostToDevice)) &&
              check(hipDeviceSynchronize()) && check(hipIpcGetMemHandle(&handle, memory));
    int fds[2];
    if (!ok || pipe(fds) != 0) { hipFree(memory); return 1; }
    char fdarg[32], gpuarg[32];
    std::snprintf(fdarg, sizeof(fdarg), "%d", fds[0]);
    std::snprintf(gpuarg, sizeof(gpuarg), "%d", 1 - source);
    pid_t child = fork();
    if (child == 0) {
      close(fds[1]);
      execl(argv[0], argv[0], "--import", fdarg, gpuarg, static_cast<char*>(nullptr));
      _exit(127);
    }
    close(fds[0]);
    if (child < 0) { close(fds[1]); hipFree(memory); return 1; }
    ok = transfer(fds[1], &handle, sizeof(handle), true);
    close(fds[1]);
    int status = 0;
    pid_t waited;
    do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
    ok = ok && waited == child && WIFEXITED(status) && WEXITSTATUS(status) == 0;
    ok = check(hipFree(memory)) && ok;
    char pci[32]{};
    if (!check(hipDeviceGetPCIBusId(pci, sizeof(pci), source))) return 1;
    std::printf("IPC exporter=%d pci=%s importer=%d bytes=%zu correctness=%s transport=unqualified\n",
                source, pci, 1-source, bytes, ok ? "passed" : "failed");
    if (!ok) return 1;
  }
  return 0;
}

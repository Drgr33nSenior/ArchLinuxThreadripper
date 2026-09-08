#include <hip/hip_runtime.h>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <vector>

namespace {

constexpr std::size_t kCopyBytes = 16U * 1024U * 1024U;
constexpr int kExitFailure = 1;
constexpr int kExitUsage = 2;
constexpr int kExitSkipped = 77;

struct DeviceInfo {
  int ordinal;
  char pci_bus_id[32];
  char uuid[33];
};

bool report_hip_error(const char* operation, hipError_t error) {
  std::fprintf(stderr, "ERROR operation=%s hip_error=%s\n", operation,
               hipGetErrorString(error));
  return false;
}

bool check_hip(const char* operation, hipError_t error) {
  return error == hipSuccess || report_hip_error(operation, error);
}

void format_uuid(const hipUUID& uuid, char* output, std::size_t output_size) {
  if (output_size < 33U) {
    return;
  }
  for (std::size_t i = 0; i < 16U; ++i) {
    std::snprintf(output + (i * 2U), output_size - (i * 2U), "%02x",
                  static_cast<unsigned char>(uuid.bytes[i]));
  }
}

void report_pair(const DeviceInfo& source, const DeviceInfo& destination,
                 int capability, const char* enable, const char* result,
                 const char* correctness, double elapsed_ms,
                 const char* detail) {
  std::printf(
      "PAIR source=%d source_uuid=%s source_pci=%s destination=%d "
      "destination_uuid=%s destination_pci=%s capability=%d peer_enable=%s "
      "result=%s correctness=%s elapsed_ms=%.3f bytes=%zu detail=%s\n",
      source.ordinal, source.uuid, source.pci_bus_id, destination.ordinal,
      destination.uuid, destination.pci_bus_id, capability, enable, result,
      correctness, elapsed_ms, kCopyBytes, detail);
}

bool run_pair(const DeviceInfo& source, const DeviceInfo& destination) {
  std::vector<unsigned char> expected(kCopyBytes);
  std::vector<unsigned char> actual(kCopyBytes);
  for (std::size_t i = 0; i < expected.size(); ++i) {
    expected[i] = static_cast<unsigned char>((i * 131U + source.ordinal * 17U +
                                               destination.ordinal) %
                                              251U);
  }

  void* source_memory = nullptr;
  void* destination_memory = nullptr;
  bool enabled_by_this_process = false;
  bool ok = true;
  const char* enable_result = "not-attempted";
  const char* detail = "copy-not-attempted";
  double elapsed_ms = 0.0;

  if (!check_hip("hipSetDevice(destination)",
                 hipSetDevice(destination.ordinal))) {
    ok = false;
    detail = "set-destination-failed";
    goto cleanup;
  }
  {
    const hipError_t enable = hipDeviceEnablePeerAccess(source.ordinal, 0);
    if (enable == hipSuccess) {
      enabled_by_this_process = true;
      enable_result = "enabled";
    } else if (enable == hipErrorPeerAccessAlreadyEnabled) {
      enable_result = "already-enabled";
    } else {
      report_hip_error("hipDeviceEnablePeerAccess", enable);
      ok = false;
      detail = "peer-enable-failed";
      goto cleanup;
    }
  }

  if (!check_hip("hipSetDevice(source)", hipSetDevice(source.ordinal)) ||
      !check_hip("hipMalloc(source)", hipMalloc(&source_memory, kCopyBytes)) ||
      !check_hip("hipMemcpy(host-to-source)",
                 hipMemcpy(source_memory, expected.data(), kCopyBytes,
                           hipMemcpyHostToDevice))) {
    ok = false;
    detail = "source-setup-failed";
    goto cleanup;
  }
  if (!check_hip("hipSetDevice(destination)",
                 hipSetDevice(destination.ordinal)) ||
      !check_hip("hipMalloc(destination)",
                 hipMalloc(&destination_memory, kCopyBytes))) {
    ok = false;
    detail = "destination-setup-failed";
    goto cleanup;
  }

  {
    const auto started = std::chrono::steady_clock::now();
    if (!check_hip("hipMemcpyPeer",
                   hipMemcpyPeer(destination_memory, destination.ordinal,
                                 source_memory, source.ordinal, kCopyBytes)) ||
        !check_hip("hipDeviceSynchronize", hipDeviceSynchronize())) {
      ok = false;
      detail = "peer-copy-failed";
      goto cleanup;
    }
    const auto completed = std::chrono::steady_clock::now();
    elapsed_ms = std::chrono::duration<double, std::milli>(completed - started).count();
  }
  if (!check_hip("hipMemcpy(destination-to-host)",
                 hipMemcpy(actual.data(), destination_memory, kCopyBytes,
                           hipMemcpyDeviceToHost))) {
    ok = false;
    detail = "readback-failed";
    goto cleanup;
  }
  if (std::memcmp(expected.data(), actual.data(), kCopyBytes) != 0) {
    ok = false;
    detail = "readback-mismatch";
  } else {
    detail = "readback-verified";
  }

cleanup:
  if (destination_memory != nullptr) {
    if (!check_hip("hipSetDevice(destination cleanup)",
                   hipSetDevice(destination.ordinal)) ||
        !check_hip("hipFree(destination)", hipFree(destination_memory))) {
      ok = false;
      detail = "destination-cleanup-failed";
    }
  }
  if (source_memory != nullptr) {
    if (!check_hip("hipSetDevice(source cleanup)", hipSetDevice(source.ordinal)) ||
        !check_hip("hipFree(source)", hipFree(source_memory))) {
      ok = false;
      detail = "source-cleanup-failed";
    }
  }
  if (enabled_by_this_process) {
    if (!check_hip("hipSetDevice(peer disable)",
                   hipSetDevice(destination.ordinal)) ||
        !check_hip("hipDeviceDisablePeerAccess",
                   hipDeviceDisablePeerAccess(source.ordinal))) {
      ok = false;
      detail = "peer-disable-failed";
    }
  }

  report_pair(source, destination, 1, enable_result, ok ? "PASS" : "ERROR",
              ok ? "verified" : "failed", elapsed_ms, detail);
  return ok;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc != 1) {
    std::fprintf(stderr, "usage: %s\n", argv[0]);
    return kExitUsage;
  }

  int device_count = 0;
  if (!check_hip("hipGetDeviceCount", hipGetDeviceCount(&device_count))) {
    return kExitFailure;
  }
  if (device_count < 2) {
    std::printf("SKIP result=SKIP reason=requires-at-least-two-HIP-devices devices=%d\n",
                device_count);
    return kExitSkipped;
  }

  std::vector<DeviceInfo> devices;
  devices.reserve(static_cast<std::size_t>(device_count));
  for (int ordinal = 0; ordinal < device_count; ++ordinal) {
    DeviceInfo info{};
    info.ordinal = ordinal;
    hipUUID uuid{};
    if (!check_hip("hipDeviceGetPCIBusId",
                   hipDeviceGetPCIBusId(info.pci_bus_id,
                                        static_cast<int>(sizeof(info.pci_bus_id)),
                                        ordinal)) ||
        !check_hip("hipDeviceGetUuid", hipDeviceGetUuid(&uuid, ordinal))) {
      return kExitFailure;
    }
    format_uuid(uuid, info.uuid, sizeof(info.uuid));
    std::printf("DEVICE ordinal=%d uuid=%s pci=%s\n", info.ordinal, info.uuid,
                info.pci_bus_id);
    devices.push_back(info);
  }

  int successful_pairs = 0;
  int skipped_pairs = 0;
  int failed_pairs = 0;
  for (const DeviceInfo& source : devices) {
    for (const DeviceInfo& destination : devices) {
      if (source.ordinal == destination.ordinal) {
        continue;
      }
      int capability = 0;
      if (!check_hip("hipDeviceCanAccessPeer(summary)",
                     hipDeviceCanAccessPeer(&capability, destination.ordinal,
                                            source.ordinal))) {
        ++failed_pairs;
        report_pair(source, destination, -1, "not-attempted", "ERROR",
                    "not-run", 0.0, "capability-query-failed");
        continue;
      }
      if (capability == 0) {
        ++skipped_pairs;
        report_pair(source, destination, 0, "not-attempted", "SKIP", "not-run",
                    0.0, "peer-capability-unavailable");
        continue;
      }
      if (run_pair(source, destination)) {
        ++successful_pairs;
      } else {
        ++failed_pairs;
      }
    }
  }

  std::printf("SUMMARY passed=%d skipped=%d failed=%d\n", successful_pairs,
              skipped_pairs, failed_pairs);
  if (failed_pairs != 0) {
    return kExitFailure;
  }
  if (successful_pairs == 0) {
    std::printf("SKIP result=SKIP reason=no-functional-peer-copy-pair\n");
    return kExitSkipped;
  }
  return 0;
}

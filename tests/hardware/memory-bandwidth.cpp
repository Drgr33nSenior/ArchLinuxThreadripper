// Explicit host-memory triad. Not STREAM, and not a claim about DIMM channels.
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <thread>
#include <vector>

int main(int argc, char** argv) {
  if (argc != 3) { std::fprintf(stderr, "usage: memory-bandwidth <workers 1..48> <MiB-per-array 16..1024>\n"); return 2; }
  const int workers = std::atoi(argv[1]);
  const int mib = std::atoi(argv[2]);
  if (workers < 1 || workers > 48 || mib < 16 || mib > 1024) return 2;
  const size_t n = static_cast<size_t>(mib) * 1024 * 1024 / sizeof(double);
  auto a = std::make_unique<double[]>(n);
  auto b = std::make_unique<double[]>(n);
  auto c = std::make_unique<double[]>(n);
  for (size_t i=0; i<n; ++i) { b[i] = i % 97; c[i] = i % 89; }
  std::printf("{\"workers\":%d,\"array_mib\":%d,\"allocation_policy\":\"default first-touch; no forced NUMA\",\"seconds\":[", workers, mib);
  for (int repeat=0; repeat<6; ++repeat) {
    const auto start = std::chrono::steady_clock::now();
    std::vector<std::thread> threads;
    for (int worker=0; worker<workers; ++worker) {
      threads.emplace_back([&, worker] {
        for (size_t i=n*worker/workers; i<n*(worker+1)/workers; ++i) a[i] = b[i] + 3.0*c[i];
      });
    }
    for (auto& thread : threads) thread.join();
    double seconds = std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
    if (repeat > 0) std::printf("%s%.9f", repeat == 1 ? "" : ",", seconds);
    for (size_t i=0; i<n; ++i) if (a[i] != static_cast<double>(i % 97) + 3.0*static_cast<double>(i % 89)) return 1;
  }
  std::printf("],\"nominal_bytes_per_iteration\":%zu,\"warmups\":1,\"correctness\":\"passed\",\"scope\":\"includes thread launch; traffic estimate excludes write allocation\"}\n", n*sizeof(double)*3);
  return 0;
}

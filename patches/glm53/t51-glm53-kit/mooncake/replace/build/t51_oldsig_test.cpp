// Negative test for the glm5.3 patch 04 (option i): the original-signature functions kept as wrappers.
// Test only. The functions are private members of mooncake::Client; their addresses are taken through explicit
// template instantiations (access checks do not apply there), so no header is compiled differently from the library.
// Needs mooncake_master with its HTTP metadata server on 127.0.0.1 (as in lab_test.sh) and MC_FORCE_TCP=1.
// glog is not initialized, so its lines go to stderr.
//   1. BatchWriteWhenPreferSameNode(ops, is_upsert), the original signature, called with an empty batch: its timing
//      line must have phases 2-4, no phase1_start_batch_put_us, no total_us, and complete=false.
//   2. LogBatchWriteTiming with the two start time points unknown (time_point::min(), what the wrapper passes) and with
//      all time points known: the first line is the incomplete form, the second the complete form.
//   3. CollectResults(ops), the original signature: the same results as the new one.
#include "client_service.h"

#include <chrono>
#include <cstdio>
#include <optional>
#include <thread>
#include <vector>

namespace {
using mooncake::Client;
using mooncake::ErrorCode;
using mooncake::PutOperation;
using Results = std::vector<tl::expected<void, ErrorCode>>;
using TP = std::chrono::steady_clock::time_point;

template <typename Tag, typename Tag::type M>
struct Grant {
    friend typename Tag::type get(Tag) { return M; }
};
struct OldBatchWrite { using type = Results (Client::*)(std::vector<PutOperation>&, bool); friend type get(OldBatchWrite); };
struct OldCollect { using type = Results (Client::*)(const std::vector<PutOperation>&); friend type get(OldCollect); };
struct NewCollect { using type = Results (Client::*)(const std::vector<PutOperation>&, size_t&); friend type get(NewCollect); };
struct LogTiming {
    using type = void (Client::*)(size_t, bool, uint64_t, size_t, TP, TP, TP, TP, TP, TP, const char*) const;
    friend type get(LogTiming);
};
template struct Grant<OldBatchWrite, &Client::BatchWriteWhenPreferSameNode>;
template struct Grant<OldCollect, &Client::CollectResults>;
template struct Grant<NewCollect, &Client::CollectResults>;
template struct Grant<LogTiming, &Client::LogBatchWriteTiming>;
}  // namespace

int main() {
    auto created = Client::Create("127.0.0.1", "http://127.0.0.1:8080/metadata", "tcp", std::nullopt, "127.0.0.1:50051");
    if (!created || !*created) {
        std::fprintf(stdout, "T51OLDSIG create=failed\n");
        return 2;
    }
    Client& client = **created;
    // An empty batch: nothing is written. PutOperation is defined only inside client_service.cpp, so the test cannot
    // construct or destroy a std::vector of it; an empty libstdc++ vector is three null pointers, which is what the
    // functions under test receive (by reference). It is never destroyed here.
    alignas(std::vector<int>) static unsigned char raw[sizeof(std::vector<int>)] = {};
    auto& ops = *reinterpret_cast<std::vector<PutOperation>*>(raw);

    Results r1 = (client.*get(OldBatchWrite{}))(ops, false);  // the original signature
    std::fprintf(stdout, "T51OLDSIG old_bwwpsn results=%zu\n", r1.size());
    std::fflush(stdout);

    using clock = std::chrono::steady_clock;
    const TP unknown = TP::min();
    const TP t0 = clock::now();
    std::this_thread::sleep_for(std::chrono::microseconds(300));
    const TP t1 = clock::now();
    std::this_thread::sleep_for(std::chrono::microseconds(300));
    const TP t2 = clock::now();
    std::this_thread::sleep_for(std::chrono::microseconds(300));
    const TP t3 = clock::now();
    (client.*get(LogTiming{}))(4, false, 16384, 0, unknown, unknown, t0, t1, t2, t3, "prefer_same_node_full");
    (client.*get(LogTiming{}))(4, false, 16384, 0, t0, t0, t0, t1, t2, t3, "normal_full");

    size_t failed = 0;
    Results r2 = (client.*get(OldCollect{}))(ops);          // the original signature
    Results r3 = (client.*get(NewCollect{}))(ops, failed);  // the new one
    std::fprintf(stdout, "T51OLDSIG old_collect=%zu new_collect=%zu failed=%zu\n", r2.size(), r3.size(), failed);
    std::fflush(stdout);
    std::_Exit(0);  // the client's threads are not joined: the test ends here
}

// The parsing table of PD_DIAG_LEVEL and MC_PD_TIMING (pd_timing_level.h), the same inputs as vLLM's Python test.
#include <cstdio>
#include <cstring>
#include <string>
#include "pd_timing_level.h"
int main() {
    struct Row { const char* in; int level; const char* source; } rows[] = {
        {nullptr, 3, "default"}, {"", 3, "default"}, {"3", 3, "env"}, {"2", 2, "env"}, {"1", 1, "env"},
        {"0", 3, "invalid"}, {"4", 3, "invalid"}, {"abc", 3, "invalid"}, {" 2 ", 3, "invalid"}, {"2x", 3, "invalid"}};
    int bad = 0;
    for (const auto& r : rows) {
        auto got = mooncake::pd_timing::ParseDiagLevel(r.in);
        const bool ok = got.level == r.level && std::strcmp(got.source, r.source) == 0;
        bad += !ok;
        std::printf("PD_DIAG_LEVEL=%-7s -> level %d source %-7s %s\n", r.in ? (std::string("'") + r.in + "'").c_str() : "(unset)",
                    got.level, got.source, ok ? "ok" : "WRONG");
    }
    struct Sw { const char* in; bool on; } sws[] = {{nullptr, true}, {"", true}, {"1", true}, {"0", false}, {"no", true}};
    for (const auto& w : sws) {
        const bool got = mooncake::pd_timing::ParseTimingSwitch(w.in);
        bad += got != w.on;
        std::printf("MC_PD_TIMING=%-5s -> %s %s\n", w.in ? (std::string("'") + w.in + "'").c_str() : "(unset)", got ? "on" : "off",
                    got == w.on ? "ok" : "WRONG");
    }
    std::printf("RESULT: %s\n", bad ? "WRONG" : "OK");
    return bad != 0;
}

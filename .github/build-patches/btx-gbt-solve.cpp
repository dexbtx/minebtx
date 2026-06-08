// Copyright (c) 2026 The BTX developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.
//
// btx-gbt-solve: external-miner solver. Takes block-header fields as CLI flags,
// runs SolveMatMul once with a configurable max_tries budget, and prints a
// single JSON object on stdout describing the result. Designed to be called
// by an external miner orchestrator (Python) that polls getblocktemplate and
// builds the submitblock hex.

#include <arith_uint256.h>
#include <chainparams.h>
#include <common/args.h>
#include <matmul/matmul_pow.h>
#include <matmul/matrix.h>
#include <pow.h>
#include <primitives/block.h>
#include <uint256.h>
#include <util/chaintype.h>
#include <util/strencodings.h>
#include <util/translation.h>

#include <univalue.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

const TranslateFn G_TRANSLATION_FUN{nullptr};

namespace {

constexpr uint32_t MAINNET_LIVE_LIKE_EPSILON_BITS{18U};

struct Options {
    int32_t version{0x20000000};
    uint256 prev_hash{};
    uint256 merkle_root{};
    uint32_t time{0};
    uint32_t bits{0};
    uint256 seed_a{};
    uint256 seed_b{};
    uint16_t matmul_n{512};
    uint32_t matmul_b{16};
    uint32_t matmul_r{8};
    uint32_t epsilon_bits{MAINNET_LIVE_LIKE_EPSILON_BITS};
    int32_t block_height{-1};
    uint64_t nonce_start{0};
    uint64_t max_tries{100'000'000ULL};
    // Optional 256-bit BE share target. When set the solver returns early
    // on digests <= share_target instead of the block-target derived from
    // --bits. header.nBits still uses --bits so the matmul digest stays
    // consensus-consistent — the lever pool miners need for share-tier
    // early-exit.
    std::optional<uint256> share_target;
    double max_seconds{0.0};  // 0 = no wall-clock cap
    std::optional<std::string> backend_override;
    std::optional<std::string> solver_threads_override;
    std::optional<std::string> batch_size_override;
    std::optional<std::string> async_override;
    std::optional<std::string> pool_slots_override;
    // Daemon mode: keep the process alive between jobs, read JSON job
    // payloads from stdin, emit one JSON result line per job on stdout.
    // Eliminates CUDA-context-init + cubin-load cost per slice (~5s of
    // 25s slice = 20% duty-cycle loss on the per-subprocess model).
    bool daemon_mode{false};
};

class ScopedEnvOverride {
public:
    ScopedEnvOverride(const char* name, const std::optional<std::string>& value)
        : m_name{name}
    {
        const char* prev = std::getenv(name);
        if (prev != nullptr) {
            m_had_previous = true;
            m_previous = prev;
        }
        if (value.has_value()) {
            setenv(name, value->c_str(), /*overwrite=*/1);
            m_set = true;
        }
    }
    ~ScopedEnvOverride() {
        if (!m_set) return;
        if (m_had_previous) {
            setenv(m_name, m_previous.c_str(), 1);
        } else {
            unsetenv(m_name);
        }
    }
private:
    const char* m_name;
    bool m_had_previous{false};
    bool m_set{false};
    std::string m_previous;
};

std::optional<uint64_t> ParseUintArg(std::string_view text)
{
    try {
        size_t consumed{0};
        std::string value_text{text};
        int base{10};
        if (value_text.size() > 2 && value_text[0] == '0' &&
            (value_text[1] == 'x' || value_text[1] == 'X')) {
            base = 16;
        }
        const uint64_t value = std::stoull(value_text, &consumed, base);
        if (consumed != text.size()) return std::nullopt;
        return value;
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

uint256 ParseUint256Hex(std::string_view hex, const char* arg_name)
{
    const auto parsed = uint256::FromHex(hex);
    if (!parsed.has_value()) {
        throw std::runtime_error(std::string("invalid uint256 for ") + arg_name);
    }
    return *parsed;
}

void PrintUsage(std::ostream& out)
{
    out << "Usage: btx-gbt-solve [flags]\n"
        << "Required flags (from getblocktemplate):\n"
        << "  --version <int>\n"
        << "  --prev-hash <hex64>\n"
        << "  --merkle-root <hex64>\n"
        << "  --time <uint32>\n"
        << "  --bits <hex compact, e.g. 1e02a876>\n"
        << "  --seed-a <hex64>\n"
        << "  --seed-b <hex64>\n"
        << "  --block-height <int>\n"
        << "Optional:\n"
        << "  --matmul-n <uint16>     default 512\n"
        << "  --matmul-b <uint32>     default 16\n"
        << "  --matmul-r <uint32>     default 8\n"
        << "  --epsilon-bits <uint32> default 18\n"
        << "  --share-target <hex64>  optional looser target for pool share-tier early-exit\n"
        << "  --nonce-start <uint64>  default 1\n"
        << "  --max-tries <uint64>    default 100,000,000\n"
        << "  --max-seconds <double>  default 0 (no cap)\n"
        << "  --backend <cpu|cuda|metal|mlx>\n"
        << "  --solver-threads <N>\n"
        << "  --batch-size <N>\n"
        << "  --async <0|1>\n"
        << "  --pool-slots <N>\n"
        << "  --daemon                stay alive, read JSON jobs from stdin\n"
        << "Outputs ONE JSON line on stdout, see source for schema.\n"
        << "In --daemon mode, ignores required CLI fields; each stdin\n"
        << "line is a job: {version,prev_hash,merkle_root,time,bits,\n"
        << "share_target?,seed_a,seed_b,block_height,nonce_start,\n"
        << "max_tries,max_seconds}.\n";
}

bool ParseArgs(int argc, char* argv[], Options& options)
{
    auto need_value = [&](int& i, const std::string& arg) -> const char* {
        if (i + 1 >= argc) {
            std::cerr << "error: " << arg << " requires a value\n";
            return nullptr;
        }
        return argv[++i];
    };

    bool got_required[7] = {false, false, false, false, false, false, false};
    auto required_idx = [&](std::string_view name) -> int {
        if (name == "--version")      return 0;
        if (name == "--prev-hash")    return 1;
        if (name == "--merkle-root")  return 2;
        if (name == "--time")         return 3;
        if (name == "--bits")         return 4;
        if (name == "--seed-a")       return 5;
        if (name == "--seed-b")       return 6;
        return -1;
    };

    for (int i = 1; i < argc; ++i) {
        const std::string arg{argv[i]};
        if (arg == "-h" || arg == "--help") { PrintUsage(std::cout); return false; }

        const char* val = nullptr;
        if (arg == "--version")           { val = need_value(i, arg); if (!val) return false; options.version = static_cast<int32_t>(*ParseUintArg(val)); got_required[required_idx(arg)] = true; }
        else if (arg == "--prev-hash")    { val = need_value(i, arg); if (!val) return false; options.prev_hash = ParseUint256Hex(val, "--prev-hash"); got_required[required_idx(arg)] = true; }
        else if (arg == "--merkle-root")  { val = need_value(i, arg); if (!val) return false; options.merkle_root = ParseUint256Hex(val, "--merkle-root"); got_required[required_idx(arg)] = true; }
        else if (arg == "--time")         { val = need_value(i, arg); if (!val) return false; options.time = static_cast<uint32_t>(*ParseUintArg(val)); got_required[required_idx(arg)] = true; }
        else if (arg == "--bits")         { val = need_value(i, arg); if (!val) return false; options.bits = static_cast<uint32_t>(*ParseUintArg(val)); got_required[required_idx(arg)] = true; }
        else if (arg == "--seed-a")       { val = need_value(i, arg); if (!val) return false; options.seed_a = ParseUint256Hex(val, "--seed-a"); got_required[required_idx(arg)] = true; }
        else if (arg == "--seed-b")       { val = need_value(i, arg); if (!val) return false; options.seed_b = ParseUint256Hex(val, "--seed-b"); got_required[required_idx(arg)] = true; }
        else if (arg == "--matmul-n")     { val = need_value(i, arg); if (!val) return false; options.matmul_n = static_cast<uint16_t>(*ParseUintArg(val)); }
        else if (arg == "--matmul-b")     { val = need_value(i, arg); if (!val) return false; options.matmul_b = static_cast<uint32_t>(*ParseUintArg(val)); }
        else if (arg == "--matmul-r")     { val = need_value(i, arg); if (!val) return false; options.matmul_r = static_cast<uint32_t>(*ParseUintArg(val)); }
        else if (arg == "--epsilon-bits") { val = need_value(i, arg); if (!val) return false; options.epsilon_bits = static_cast<uint32_t>(*ParseUintArg(val)); }
        else if (arg == "--share-target") { val = need_value(i, arg); if (!val) return false; options.share_target = ParseUint256Hex(val, "--share-target"); }
        else if (arg == "--block-height") { val = need_value(i, arg); if (!val) return false; options.block_height = static_cast<int32_t>(*ParseUintArg(val)); }
        else if (arg == "--nonce-start")  { val = need_value(i, arg); if (!val) return false; options.nonce_start = *ParseUintArg(val); }
        else if (arg == "--max-tries")    { val = need_value(i, arg); if (!val) return false; options.max_tries = *ParseUintArg(val); }
        else if (arg == "--max-seconds")  { val = need_value(i, arg); if (!val) return false; options.max_seconds = std::stod(val); }
        else if (arg == "--backend")            { val = need_value(i, arg); if (!val) return false; options.backend_override = val; }
        else if (arg == "--solver-threads")     { val = need_value(i, arg); if (!val) return false; options.solver_threads_override = val; }
        else if (arg == "--batch-size")         { val = need_value(i, arg); if (!val) return false; options.batch_size_override = val; }
        else if (arg == "--async")              { val = need_value(i, arg); if (!val) return false; options.async_override = val; }
        else if (arg == "--pool-slots")         { val = need_value(i, arg); if (!val) return false; options.pool_slots_override = val; }
        else if (arg == "--daemon")             { options.daemon_mode = true; }
        else {
            std::cerr << "error: unknown arg: " << arg << "\n";
            return false;
        }
    }

    // In daemon mode required fields come per-job via stdin JSON, not CLI.
    if (!options.daemon_mode) {
        for (int i = 0; i < 7; ++i) {
            if (!got_required[i]) {
                std::cerr << "error: missing required arg #" << i << "\n";
                PrintUsage(std::cerr);
                return false;
            }
        }
    }
    return true;
}

std::string HexBytesLE32(const std::vector<uint32_t>& v)
{
    // Each uint32 -> 4 LE bytes -> 8 hex chars. Total 8 * N hex chars.
    std::string out;
    out.reserve(v.size() * 8);
    static const char* hex = "0123456789abcdef";
    for (uint32_t value : v) {
        for (int byte = 0; byte < 4; ++byte) {
            uint8_t b = static_cast<uint8_t>((value >> (byte * 8)) & 0xFF);
            out.push_back(hex[b >> 4]);
            out.push_back(hex[b & 0xF]);
        }
    }
    return out;
}

} // namespace

// Run one solve against the parameters in `options`. Writes one JSON
// result line to stdout. Returns whether a solution (share-target or block-
// target) was found. Used by both one-shot and daemon-mode paths.
bool RunOneJob(Options& options, const Consensus::Params& consensus)
{
    CBlockHeader header{};
    header.nVersion = options.version;
    header.hashPrevBlock = options.prev_hash;
    header.hashMerkleRoot = options.merkle_root;
    header.nTime = options.time;
    header.nBits = options.bits;
    header.nNonce64 = options.nonce_start;
    header.nNonce = static_cast<uint32_t>(options.nonce_start);
    header.matmul_dim = options.matmul_n;
    header.seed_a = options.seed_a;
    header.seed_b = options.seed_b;
    header.matmul_digest.SetNull();

    std::atomic<bool> abort_flag{false};
    std::vector<uint32_t> matrix_c_data;

    std::thread watchdog;
    if (options.max_seconds > 0.0) {
        watchdog = std::thread([&abort_flag, max_seconds = options.max_seconds]() {
            const auto deadline = std::chrono::steady_clock::now() +
                std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                    std::chrono::duration<double>(max_seconds));
            while (std::chrono::steady_clock::now() < deadline) {
                if (abort_flag.load(std::memory_order_relaxed)) return;
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
            abort_flag.store(true, std::memory_order_relaxed);
        });
    }

    const auto start = std::chrono::steady_clock::now();
    uint64_t tries_budget = options.max_tries;
    const uint256* share_target_ptr = options.share_target ? &(*options.share_target) : nullptr;
    const bool found = SolveMatMul(header, consensus, tries_budget,
                                   options.block_height, &abort_flag, &matrix_c_data,
                                   share_target_ptr);
    const auto stop = std::chrono::steady_clock::now();
    const double elapsed_s = std::chrono::duration<double>(stop - start).count();
    const uint64_t tries_used = options.max_tries - tries_budget;

    abort_flag.store(true, std::memory_order_relaxed);
    if (watchdog.joinable()) watchdog.join();

    UniValue out(UniValue::VOBJ);
    out.pushKV("found", found);
    out.pushKV("tries_used", tries_used);
    out.pushKV("elapsed_s", elapsed_s);
    out.pushKV("nonce64_end", header.nNonce64);
    if (found) {
        out.pushKV("nonce64", header.nNonce64);
        out.pushKV("matmul_digest", header.matmul_digest.GetHex());
        out.pushKV("matrix_c_data_hex", HexBytesLE32(matrix_c_data));
        out.pushKV("matrix_c_data_words", static_cast<uint64_t>(matrix_c_data.size()));
        const auto block_target = DeriveTarget(options.bits, consensus.powLimit);
        const bool is_block = block_target
            ? UintToArith256(header.matmul_digest) <= *block_target
            : false;
        out.pushKV("is_block", is_block);
    } else {
        std::string reason{"max_tries_exhausted"};
        if (abort_flag.load() && options.max_seconds > 0.0 && elapsed_s >= options.max_seconds) {
            reason = "max_seconds_exceeded";
        }
        out.pushKV("reason", reason);
    }

    std::cout << out.write() << std::endl;
    return found;
}

// Parse a JSON job payload (one line, per the protocol in PrintUsage) and
// overwrite per-job fields of `options`. Daemon mode only. Throws on bad
// uint256 hex (caught at caller, emitted as error JSON). Returns false on
// malformed JSON envelope.
bool UpdateJobFromJson(const std::string& line, Options& options)
{
    UniValue job;
    if (!job.read(line) || !job.isObject()) return false;
    if (job.exists("version")) options.version = static_cast<int32_t>(job["version"].getInt<int64_t>());
    if (job.exists("prev_hash"))    options.prev_hash    = ParseUint256Hex(job["prev_hash"].get_str(), "prev_hash");
    if (job.exists("merkle_root"))  options.merkle_root  = ParseUint256Hex(job["merkle_root"].get_str(), "merkle_root");
    if (job.exists("time"))         options.time         = static_cast<uint32_t>(job["time"].getInt<int64_t>());
    if (job.exists("bits")) {
        const std::string bs = job["bits"].get_str();
        const std::string h  = (bs.rfind("0x", 0) == 0 || bs.rfind("0X", 0) == 0) ? bs.substr(2) : bs;
        options.bits = static_cast<uint32_t>(std::stoull(h, nullptr, 16));
    }
    if (job.exists("seed_a"))       options.seed_a       = ParseUint256Hex(job["seed_a"].get_str(), "seed_a");
    if (job.exists("seed_b"))       options.seed_b       = ParseUint256Hex(job["seed_b"].get_str(), "seed_b");
    if (job.exists("block_height")) options.block_height = static_cast<int32_t>(job["block_height"].getInt<int64_t>());
    if (job.exists("nonce_start"))  options.nonce_start  = static_cast<uint64_t>(job["nonce_start"].getInt<int64_t>());
    if (job.exists("max_tries"))    options.max_tries    = static_cast<uint64_t>(job["max_tries"].getInt<int64_t>());
    if (job.exists("max_seconds"))  options.max_seconds  = job["max_seconds"].get_real();
    if (job.exists("share_target") && job["share_target"].isStr() && !job["share_target"].get_str().empty()) {
        options.share_target = ParseUint256Hex(job["share_target"].get_str(), "share_target");
    } else {
        options.share_target.reset();
    }
    return true;
}

int main(int argc, char* argv[])
{
    Options options;
    try {
        if (!ParseArgs(argc, argv, options)) return 1;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }

    // Apply env overrides ONCE for the lifetime of this process. In daemon
    // mode these stick across all jobs so the matmul backend (CUDA in
    // particular) keeps its initialized context + loaded cubins.
    ScopedEnvOverride backend_env("BTX_MATMUL_BACKEND", options.backend_override);
    ScopedEnvOverride solver_threads_env("BTX_MATMUL_SOLVER_THREADS", options.solver_threads_override);
    ScopedEnvOverride batch_env("BTX_MATMUL_SOLVE_BATCH_SIZE", options.batch_size_override);
    ScopedEnvOverride async_env("BTX_MATMUL_PIPELINE_ASYNC", options.async_override);
    ScopedEnvOverride pool_env("BTX_MATMUL_CUDA_POOL_SLOTS", options.pool_slots_override);
    ScopedEnvOverride pool_env_metal("BTX_MATMUL_METAL_POOL_SLOTS", options.pool_slots_override);

    ArgsManager args;
    auto consensus = CreateChainParams(args, ChainType::REGTEST)->GetConsensus();
    consensus.fMatMulPOW = true;
    consensus.nMatMulDimension = options.matmul_n;
    consensus.nMatMulTranscriptBlockSize = options.matmul_b;
    consensus.nMatMulNoiseRank = options.matmul_r;
    consensus.nMatMulPreHashEpsilonBits = options.epsilon_bits;
    consensus.powLimit = uint256{"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"};
    // Mainnet MatMul nonce-seed V2 activation. At height >= this, SolveMatMul
    // routes through SolveMatMulNonceSeeded which re-derives per-nonce seed_a/
    // seed_b from the (mutable) header via DeterministicMatMulSeedV2. Without
    // this, the miner would use pool-provided static seeds post-fork and the
    // pool's per-nonce digest recomputation would mismatch every share.
    consensus.nMatMulNonceSeedHeight = 125'000;

    if (!options.daemon_mode) {
        // One-shot path: run a single job from the CLI-parsed options, exit.
        const bool found = RunOneJob(options, consensus);
        return found ? 0 : 2;
    }

    // Daemon mode: emit a ready marker on stderr (cheap handshake for the
    // wrapper), then read one JSON job per line on stdin. Each job updates
    // the per-job fields of `options` and runs RunOneJob, which emits one
    // JSON line per job on stdout. EOF on stdin terminates cleanly.
    std::cerr << "{\"event\":\"daemon_ready\"}" << std::endl;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;
        try {
            if (!UpdateJobFromJson(line, options)) {
                UniValue err(UniValue::VOBJ);
                err.pushKV("error", "bad job json envelope");
                std::cout << err.write() << std::endl;
                std::cout.flush();
                continue;
            }
            RunOneJob(options, consensus);
        } catch (const std::exception& e) {
            UniValue err(UniValue::VOBJ);
            err.pushKV("error", std::string("job exception: ") + e.what());
            std::cout << err.write() << std::endl;
        }
        std::cout.flush();
    }
    return 0;
}

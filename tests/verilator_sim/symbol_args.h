// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#ifndef TESTS_VERILATOR_SIM_SYMBOL_ARGS_H_
#define TESTS_VERILATOR_SIM_SYMBOL_ARGS_H_

// Shared parsing for the simulators' --set/--dump flags.
//
//   --set=active_m=1,active_k=640,active_epsilon=f32:1e-6
//       32-bit values written to ELF symbols before the core starts. A value
//       is an integer (decimal or 0x hex) or f32:<float> for float variables.
//   --dump=cycle_count
//       32-bit values read from ELF symbols after the run, printed as
//       "SYMBOL cycle_count=1234".

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace coralnpu_sim {

inline std::optional<std::pair<std::string, uint32_t>> ParseSymbolAssignment(
    const std::string& arg) {
  const size_t eq = arg.find('=');
  if (eq == std::string::npos || eq == 0 || eq + 1 == arg.size()) {
    return std::nullopt;
  }
  const std::string name = arg.substr(0, eq);
  const std::string value = arg.substr(eq + 1);
  char* end = nullptr;
  uint32_t bits = 0;
  if (value.rfind("f32:", 0) == 0) {
    const float f = std::strtof(value.c_str() + 4, &end);
    std::memcpy(&bits, &f, sizeof(bits));
  } else {
    const long long v = std::strtoll(value.c_str(), &end, 0);
    bits = static_cast<uint32_t>(v);
  }
  if (end == nullptr || *end != '\0') return std::nullopt;
  return std::make_pair(name, bits);
}

}  // namespace coralnpu_sim

#endif  // TESTS_VERILATOR_SIM_SYMBOL_ARGS_H_

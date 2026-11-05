#pragma once
/* Durable node state for MCU storage (NVS blob), and the sequence floors of
 * ADR 0023. The byte form is not the Python state.json shape; it carries
 * the same fields under the same validation (PersistedState.from_json):
 *
 *   offset  size  field
 *   0       4     magic "YWS" and format version 1
 *   4       8     seq, the reserved outbound ceiling (u64 little endian)
 *   12      8     pairing version counter n (u64 little endian)
 *   20      1+P   producer: length P (0 = null) and id bytes
 *   ..      1+O   version origin: length O and id bytes ("" iff n == 0)
 *   ..      4     CRC-32 (IEEE 802.3, little endian) of all bytes above
 *
 * At most YWP_SAVED_MAX bytes. Decoding is strict: a short, oversized,
 * CRC-failing or semantically invalid blob is corrupt, and corrupt state
 * must never become fresh state (the shell requires explicit recovery). */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "ywp_types.h"

#define YWP_SAVED_MAX (4 + 8 + 8 + 1 + YWP_ID_MAX + 1 + YWP_ID_MAX + 4)

/* seq and n <= 2^63-1; ids valid; n == 0 iff origin "" and then unpaired. */
bool ywp_saved_valid(const ywp_saved_t *state);
/* Returns the encoded length, or 0 when the state is invalid. */
size_t ywp_saved_encode(const ywp_saved_t *state, uint8_t out[YWP_SAVED_MAX]);
bool ywp_saved_decode(const uint8_t *data, size_t len, ywp_saved_t *state);

/* Restart floor: max(S, min(W, S + 60 s)) with W = floor(wall) * 1e9, or W
 * alone when S == 0 (first commissioning). Saturates at UINT64_MAX. */
uint64_t ywp_restart_floor(uint64_t saved, double wall);
/* Highest sequence a receiver at Unix time `wall` admits:
 * (floor(wall) + 3600) * 1e9, saturating. It is also the bound saved by the
 * explicit `--rebase-sequence` recovery (ADR 0023). */
uint64_t ywp_admission_bound(double wall);

#pragma once
/* JSON number semantics of the YWP/1 reference implementation, which is
 * Python: float() of a literal (correctly rounded, ties to even) and
 * repr() of a double (shortest round-trip digits, Python layout). Used for
 * the frame finiteness rule, the legacy canonical re-serialization and the
 * canonical encoder (doc/design/PROTOCOL.md, Encoding).
 *
 * Pure C11: no allocation, no libm, no locale, no libc number conversion.
 * Every result is identical on every platform with IEEE-754 binary64. */
#include <stdbool.h>
#include <stddef.h>

/* Longest repr is "-2.2250738585072014e-308" (24 bytes); plus NUL. */
#define YWP_REPR_MAX 32

/* Convert a JSON number literal to the nearest double, ties to even.
 * `lit` must match the RFC 8259 number grammar (the JSON scanner has
 * already checked it); integers and literals of any length (a frame holds
 * up to ~1200 digits) are accepted. Returns false when the value rounds to
 * infinity, which Python rejects (float() overflow, or OverflowError when
 * converting an int); *out is then unspecified. Underflow yields a
 * correctly rounded subnormal or a signed zero. */
bool ywp_num_parse(const char *lit, size_t len, double *out);

/* Python repr() of a finite double, NUL-terminated; returns the length.
 * Shortest digits that round-trip; fixed notation when the decimal
 * exponent is in [-4, 16), else d[.ddd]e±XX with at least two exponent
 * digits; integral fixed values end in ".0"; -0.0 is "-0.0". */
size_t ywp_num_repr(double value, char out[YWP_REPR_MAX]);

/* True iff float(lit) is finite (Python). Decided from the decimal digits and
 * exponent alone: the exact comparison with 2^1024 - 2^970 runs only for
 * literals whose leading digit sits at 10^308; no binary conversion. False
 * for text that is not a JSON number. */
bool ywp_num_finite(const char *lit, size_t len);

/* repr(float(lit)) (the JSON canonical spelling of a number with a fraction
 * or exponent), NUL-terminated; returns its length. Meant for finite
 * literals; an infinite one gives "inf" or "-inf", text that is not a JSON
 * number gives "" (0). Each literal is converted at most once, in bounded
 * time: no conversion at all for up to 15 significant digits in
 * [1e-307, DBL_MAX]. */
size_t ywp_num_canonical(const char *lit, size_t len, char out[YWP_REPR_MAX]);

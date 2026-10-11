#!/bin/sh
# Build only the upstream sensor driver against a PREPARED vendor kernel build.
set -eu
if [ "$#" -lt 2 ]; then
    echo "Usage: $0 KERNEL_SOURCE KERNEL_BUILD [OUTPUT_DIR]" >&2
    echo "Use the exact vendor commit/config and Module.symvers of the target board." >&2
    exit 2
fi
DHT_SOURCE=$(cd "$1" && pwd)
DHT_BUILD=$(cd "$2" && pwd)
DHT_OUTPUT=${3:-./dht11-output}
mkdir -p "$DHT_OUTPUT"
DHT_OUTPUT=$(cd "$DHT_OUTPUT" && pwd)
[ -f "$DHT_SOURCE/drivers/iio/humidity/dht11.c" ] || { echo "Missing upstream dht11.c" >&2; exit 1; }
[ -f "$DHT_BUILD/Module.symvers" ] || { echo "Missing Module.symvers: modules_prepare alone is insufficient for a verified vendor build" >&2; exit 1; }
[ -f "$DHT_BUILD/include/generated/utsrelease.h" ] || { echo "Kernel build is not prepared" >&2; exit 1; }
[ -f "$DHT_BUILD/.config" ] || { echo "Kernel configuration missing" >&2; exit 1; }
DHT_RELEASE=$(sed -n 's/^#define UTS_RELEASE "\(.*\)"$/\1/p' "$DHT_BUILD/include/generated/utsrelease.h")
[ -n "$DHT_RELEASE" ] || { echo "Kernel release missing" >&2; exit 1; }
grep -Eq '^CONFIG_IIO=(y|m)$' "$DHT_BUILD/.config" || { echo "CONFIG_IIO required" >&2; exit 1; }
grep -q '^CONFIG_MODULES=y$' "$DHT_BUILD/.config" || { echo "CONFIG_MODULES required" >&2; exit 1; }
cp "$DHT_SOURCE/drivers/iio/humidity/dht11.c" "$DHT_OUTPUT/dht11.c"
DHT_HELPER=$(cd "$(dirname "$0")" && pwd)
patch --batch --forward -d "$DHT_OUTPUT" -p1 < "$DHT_HELPER/dht11-bounded-polling.patch"
printf '%s\n' 'obj-m := dht11.o' > "$DHT_OUTPUT/Makefile"
make -C "$DHT_BUILD" ARCH=arm64 CROSS_COMPILE="${CROSS_COMPILE:-aarch64-linux-gnu-}" modules_prepare
make -C "$DHT_BUILD" ARCH=arm64 CROSS_COMPILE="${CROSS_COMPILE:-aarch64-linux-gnu-}" M="$DHT_OUTPUT" modules
if command -v modinfo >/dev/null 2>&1; then
    modinfo -F name "$DHT_OUTPUT/dht11.ko"
    modinfo -F vermagic "$DHT_OUTPUT/dht11.ko"
    DHT_MODULE_RELEASE=$(modinfo -F vermagic "$DHT_OUTPUT/dht11.ko" | cut -d ' ' -f 1)
    [ "$DHT_MODULE_RELEASE" = "$DHT_RELEASE" ] || {
        echo "Kernel release changed during build; use the original LOCALVERSION (especially with archived source)" >&2
        exit 1
    }
fi
printf 'Module ready: %s/dht11.ko\n' "$DHT_OUTPUT"

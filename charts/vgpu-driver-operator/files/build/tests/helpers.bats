#!/usr/bin/env bats

setup() {
    export DRIVER_VERSION=535.261.03
    export NVIDIA_DRIVER_LIB_ONLY=1
    # shellcheck source=../nvidia-driver
    source "${BATS_TEST_DIRNAME}/../nvidia-driver"
}

write_modinfo_mock() {
    mkdir -p "${BATS_TEST_TMPDIR}/bin"
    cat > "${BATS_TEST_TMPDIR}/bin/modinfo" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
field=$2
module=${3##*/}
case "${field}:${module}" in
    vermagic:nvidia.ko)       printf '%s%s\n' "${MOCK_VERMAGIC-${KERNEL_VERSION}}" "${MOCK_VERMAGIC_SUFFIX- SMP preempt mod_unload}" ;;
    vermagic:nvidia-modeset.ko) printf '%s%s\n' "${MOCK_VERMAGIC-${KERNEL_VERSION}}" "${MOCK_VERMAGIC_SUFFIX- SMP preempt mod_unload}" ;;
    vermagic:nvidia-uvm.ko)   printf '%s%s\n' "${MOCK_VERMAGIC-${KERNEL_VERSION}}" "${MOCK_VERMAGIC_SUFFIX- SMP preempt mod_unload}" ;;
    name:nvidia.ko)           printf '%s\n' "${MOCK_NVIDIA_NAME:-nvidia}" ;;
    name:nvidia-modeset.ko)   printf '%s\n' "${MOCK_MODESET_NAME:-nvidia_modeset}" ;;
    name:nvidia-uvm.ko)       printf '%s\n' "${MOCK_UVM_NAME:-nvidia_uvm}" ;;
esac
EOF
    chmod +x "${BATS_TEST_TMPDIR}/bin/modinfo"
    export PATH="${BATS_TEST_TMPDIR}/bin:${PATH}"
}

stage_modules() {
    mkdir -p "$1"
    touch "$1/nvidia.ko" "$1/nvidia-modeset.ko" "$1/nvidia-uvm.ko"
}

write_symbol_dependency_mocks() {
    mkdir -p "${BATS_TEST_TMPDIR}/bin"
    cat > "${BATS_TEST_TMPDIR}/bin/modinfo" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "-k" ] && [ "$3" = "-n" ]; then
    case "$4" in
        video)     printf '%s/%s/kernel/drivers/acpi/video.ko.xz\n' "${KERNEL_MODULES_ROOT:-/lib/modules}" "$2" ;;
        backlight) printf '%s/%s/kernel/drivers/video/backlight/backlight.ko.xz\n' "${KERNEL_MODULES_ROOT:-/lib/modules}" "$2" ;;
        *)         exit 1 ;;
    esac
    exit 0
fi
exec /usr/bin/modinfo "$@"
EOF
    chmod +x "${BATS_TEST_TMPDIR}/bin/modinfo"
    cat > "${BATS_TEST_TMPDIR}/bin/nm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${*: -1}" in
    *nvidia-modeset.ko)
        printf '                 U acpi_video_register_backlight\n'
        printf '                 U backlight_device_get_by_type\n'
        ;;
esac
EOF
    chmod +x "${BATS_TEST_TMPDIR}/bin/nm"
    export PATH="${BATS_TEST_TMPDIR}/bin:${PATH}"
}

@test "_log emits RFC3339 timestamp + level + message" {
    run _log INFO hello world
    [ "$status" -eq 0 ]
    [[ "${output}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\ \[INFO\]\ hello\ world$ ]]
}

@test "_log accepts arbitrary level token" {
    run _log WARN something
    [[ "${output}" =~ \[WARN\] ]]
}

@test "_detect_driver_arch maps x86_64" {
    run _detect_driver_arch x86_64
    [ "$output" = "x86_64" ]
}

@test "_detect_driver_arch maps amd64 to x86_64" {
    run _detect_driver_arch amd64
    [ "$output" = "x86_64" ]
}

@test "_detect_driver_arch maps aarch64" {
    run _detect_driver_arch aarch64
    [ "$output" = "aarch64" ]
}

@test "_detect_driver_arch maps arm64 to aarch64" {
    run _detect_driver_arch arm64
    [ "$output" = "aarch64" ]
}

@test "_detect_driver_arch defaults unknown to x86_64" {
    run _detect_driver_arch riscv64
    [ "$output" = "x86_64" ]
}

@test "DRIVER_ARCH derives from TARGETARCH override" {
    TARGETARCH=arm64 NVIDIA_DRIVER_LIB_ONLY=1 DRIVER_VERSION=1 \
        bash -c 'source "'"${BATS_TEST_DIRNAME}"'/../nvidia-driver"; echo "${DRIVER_ARCH}"' \
        | grep -qx aarch64
}

@test "_validate_precompiled_modules accepts valid module metadata" {
    export KERNEL_VERSION=6.12.81-flatcar
    local install_dir="${BATS_TEST_TMPDIR}/mods"
    stage_modules "${install_dir}"
    write_modinfo_mock

    run _validate_precompiled_modules "${install_dir}"
    [ "$status" -eq 0 ]
}

@test "_validate_precompiled_modules rejects missing vermagic" {
    export KERNEL_VERSION=6.12.81-flatcar
    export MOCK_VERMAGIC=""
    export MOCK_VERMAGIC_SUFFIX=""
    local install_dir="${BATS_TEST_TMPDIR}/mods"
    stage_modules "${install_dir}"
    write_modinfo_mock

    run _validate_precompiled_modules "${install_dir}"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Invalid NVIDIA module metadata for nvidia.ko: missing vermagic"* ]]
}

@test "_validate_precompiled_modules rejects wrong vermagic prefix" {
    export KERNEL_VERSION=6.12.81-flatcar
    export MOCK_VERMAGIC=6.12.80-flatcar
    local install_dir="${BATS_TEST_TMPDIR}/mods"
    stage_modules "${install_dir}"
    write_modinfo_mock

    run _validate_precompiled_modules "${install_dir}"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Invalid NVIDIA module metadata for nvidia.ko: vermagic '6.12.80-flatcar SMP preempt mod_unload' does not match '6.12.81-flatcar '"* ]]
}

@test "_validate_precompiled_modules rejects wrong module name" {
    export KERNEL_VERSION=6.12.81-flatcar
    export MOCK_MODESET_NAME=nvidia-modeset
    local install_dir="${BATS_TEST_TMPDIR}/mods"
    stage_modules "${install_dir}"
    write_modinfo_mock

    run _validate_precompiled_modules "${install_dir}"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Invalid NVIDIA module metadata for nvidia-modeset.ko: name 'nvidia-modeset' does not match 'nvidia_modeset'"* ]]
}

@test "_stage_kernel_symbol_dependencies copies provider modules and recursive deps" {
    export KERNEL_VERSION=6.12.81-flatcar
    export KERNEL_MODULES_ROOT="${BATS_TEST_TMPDIR}/lib/modules"
    local kernel_tree="${KERNEL_MODULES_ROOT}/${KERNEL_VERSION}"
    local install_dir="${BATS_TEST_TMPDIR}/install"

    mkdir -p "${kernel_tree}/kernel/drivers/acpi" \
             "${kernel_tree}/kernel/drivers/platform/x86" \
             "${kernel_tree}/kernel/drivers/video/backlight"
    touch "${kernel_tree}/kernel/drivers/acpi/video.ko.xz" \
          "${kernel_tree}/kernel/drivers/platform/x86/wmi.ko.xz" \
          "${kernel_tree}/kernel/drivers/video/backlight/backlight.ko.xz"
    cat > "${kernel_tree}/modules.dep" <<'EOF'
kernel/drivers/acpi/video.ko.xz: kernel/drivers/platform/x86/wmi.ko.xz kernel/drivers/video/backlight/backlight.ko.xz
kernel/drivers/video/backlight/backlight.ko.xz: kernel/drivers/platform/x86/wmi.ko.xz
EOF
    cat > "${kernel_tree}/modules.symbols" <<'EOF'
alias symbol:acpi_video_register_backlight video
alias symbol:backlight_device_get_by_type backlight
EOF
    stage_modules "${install_dir}"
    write_symbol_dependency_mocks

    run _stage_kernel_symbol_dependencies "${install_dir}"
    [ "$status" -eq 0 ]
    [ -f "${install_dir}/kernel/drivers/acpi/video.ko.xz" ]
    [ -f "${install_dir}/kernel/drivers/platform/x86/wmi.ko.xz" ]
    [ -f "${install_dir}/kernel/drivers/video/backlight/backlight.ko.xz" ]
}

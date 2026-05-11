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

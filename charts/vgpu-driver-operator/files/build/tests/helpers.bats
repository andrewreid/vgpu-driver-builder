#!/usr/bin/env bats

setup() {
    export DRIVER_VERSION=535.261.03
    export NVIDIA_DRIVER_LIB_ONLY=1
    # shellcheck source=../nvidia-driver
    source "${BATS_TEST_DIRNAME}/../nvidia-driver"
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

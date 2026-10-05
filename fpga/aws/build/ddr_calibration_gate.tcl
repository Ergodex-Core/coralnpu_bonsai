# DDR-enabled candidate: fail closed using the pinned HDK calibration RAM path.
# The HDK check_ddr_bram.tcl only warns/skips; this check must not do either.
proc coral_require_ddr_calibration {} {
    set cells [get_cells -hierarchical -filter {NAME=~*mcs0/inst/lmb_bram_I/U0/inst_blk_mem_gen/gnbram.gnative_mem_map_bmg.native_mem_map_blk_mem_gen/valid.cstr/ramloop[0].ram.r/prim_noinit.ram/DEVICE_8SERIES.WITH_BMM_INFO.TRUE_DP.SIMPLE_PRIM36.SERIES8_TDP_SP36_NO_ECC_ATTR.ram}]
    if {[llength $cells] != 1} {
        error "Expected exactly one DDR calibration BRAM; found [llength $cells]"
    }
    set init [get_property INIT_2C [lindex $cells 0]]
    if {![regexp {^256'h([0-9a-fA-F]{64})$} $init -> bits]} {
        error "Missing or malformed DDR calibration BRAM INIT_2C"
    }
    if {![regexp {[1-9a-fA-F]} $bits]} {
        error "DDR calibration BRAM INIT_2C is zero; calibration would fail"
    }
    return [dict create cells 1 init_2c $init]
}

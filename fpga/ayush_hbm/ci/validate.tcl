# Collect evidence from the exact DCP; Python applies the fail-closed policy.
# No source-clock-name CDC exemptions and no severity downgrades are permitted.
if {$argc != 2} { error "Expected checkpoint and report directory" }
set out [lindex $argv 1]
file mkdir $out
foreach rule [get_drc_checks] {
    set baseline($rule) [list [get_property SEVERITY $rule] [get_property IS_ENABLED $rule]]
}
open_checkpoint [lindex $argv 0]
if {[get_property PART [current_design]] ne "xcvu47p-fsvh2892-2-e"} { error "Wrong FPGA part" }
set facts [open $out/facts.tsv w]
puts $facts "fully_routed\t[report_route_status -boolean_check ROUTED_FULLY]"
puts $facts "route_errors\t[report_route_status -boolean_check ERRORS_IN_ROUTES]"
puts $facts "waivers\t[llength [get_waivers -quiet]]"
foreach {name period} {npu_clk 20.0 clk_main_a0 4.0 clk_out1_cl_hbm_mmcm 3.333333} {
    set clock [get_clocks -quiet $name]
    if {[llength $clock] != 1} { error "Missing or ambiguous clock $name" }
    set actual [get_property PERIOD $clock]
    if {abs($actual-$period) > 0.002} { error "Wrong hardware clock $name" }
    puts $facts "clock.$name\t$actual"
}
set mmcm [get_cells -hier -filter {NAME =~ */CL_HBM/*/HBM_MMCM_I/inst/mmcme4_adv_inst}]
if {[llength $mmcm] != 1} { error "Missing HBM MMCM" }
foreach {property expected} {CLKFBOUT_MULT_F 24.0 DIVCLK_DIVIDE 2 CLKOUT0_DIVIDE_F 4.0} {
    if {abs([get_property $property $mmcm]-$expected) > 0.001} { error "Wrong HBM divider" }
}
report_route_status -file $out/route_status.rpt
report_timing_summary -report_unconstrained -file $out/timing_summary.rpt
report_clocks -file $out/clocks.rpt
report_clock_interaction -file $out/clock_interaction.rpt
check_timing -verbose -file $out/check_timing.rpt
report_cdc -details -file $out/cdc.rpt
report_bus_skew -file $out/bus_skew.rpt
report_exceptions -coverage -file $out/exceptions.rpt
report_drc -file $out/drc.rpt
puts $facts "drc_violations\t[llength [get_drc_violations -quiet]]"
report_methodology -file $out/methodology.rpt
set changed 0
foreach rule [get_drc_checks] {
    set now [list [get_property SEVERITY $rule] [get_property IS_ENABLED $rule]]
    if {![info exists baseline($rule)] || $now ne $baseline($rule)} { incr changed }
}
puts $facts "changed_drc_rules\t$changed"
close $facts
write_debug_probes -no_partial_ltxfile -force $out/debug_probes.ltx
puts "HBM_REPORT_COLLECTION_COMPLETE"
close_design
exit

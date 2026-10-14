# Reopen the exact checkpoint; report failures must not be silently ignored.
if {$argc != 2} { error "Expected checkpoint and report directory" }
set checkpoint [lindex $argv 0]
set out [lindex $argv 1]
file mkdir $out
# A fresh Vivado process supplies the default severity policy. Compare after
# opening/reporting the DCP so saved rule changes cannot conceal violations.
foreach rule [get_drc_checks] {
    set default_severity($rule) [get_property SEVERITY $rule]
    set default_enabled($rule) [get_property IS_ENABLED $rule]
}
open_checkpoint $checkpoint
if {[get_property PART [current_design]] ne "xcvu47p-fsvh2892-2-e"} {
    error "Unexpected FPGA device"
}
set fully_routed [report_route_status -boolean_check ROUTED_FULLY]
set route_errors [report_route_status -boolean_check ERRORS_IN_ROUTES]
report_route_status -file $out/route_status.rpt
report_timing_summary -report_unconstrained -file $out/timing_summary.rpt
report_clocks -file $out/clocks.rpt
report_bus_skew -file $out/bus_skew.rpt
check_timing -verbose -file $out/check_timing.rpt
report_drc -file $out/drc.rpt
report_methodology -file $out/methodology.rpt
report_cdc -details -file $out/cdc.rpt
report_exceptions -coverage -file $out/exceptions.rpt
set facts [open $out/facts.tsv w]
puts $facts "fully_routed\t$fully_routed"
puts $facts "route_errors\t$route_errors"
puts $facts "existing_waivers\t[llength [get_waivers -quiet]]"
foreach {name period} {clk_main_a0 4.000 npu_clk 20.000} {
    set clock [get_clocks -quiet $name]
    if {[llength $clock] != 1} { error "Missing or ambiguous clock $name" }
    set actual [get_property PERIOD $clock]
    puts $facts "clock.$name.period_ns\t$actual"
    if {abs($actual - $period) > 0.001} { error "Unexpected period for $name: $actual" }
}
set divider [get_cells -quiet WRAPPER/CL/i_npu_clk]
if {[llength $divider] != 1 || [get_property BUFGCE_DIVIDE $divider] != 5} {
    error "Expected NPU clock divider 5"
}
set npu_clock [get_clocks npu_clk]
if {[get_property MASTER_CLOCK $npu_clock] ne "clk_main_a0"} {
    error "Unexpected NPU master clock"
}
puts $facts "drc_errors\t[llength [get_drc_violations -quiet -filter {SEVERITY == Error}]]"
set changed_severities 0
foreach rule [get_drc_checks] {
    if {![info exists default_severity($rule)] || [get_property SEVERITY $rule] ne $default_severity($rule) || [get_property IS_ENABLED $rule] ne $default_enabled($rule)} {
        incr changed_severities
    }
}
puts $facts "changed_drc_severities\t$changed_severities"
close $facts
# Keep severity configuration reviewable; report_property accepts only one rule.
set properties [open $out/drc_check_properties.rpt w]
puts $properties "rule\tseverity\tenabled\tdefault_severity\tdefault_enabled"
foreach rule [lsort [get_drc_checks]] {
    if {![info exists default_severity($rule)]} {
        set default_severity($rule) "NOT_PRESENT_BEFORE_CHECKPOINT"
        set default_enabled($rule) "NOT_PRESENT_BEFORE_CHECKPOINT"
    }
    puts $properties "$rule\t[get_property SEVERITY $rule]\t[get_property IS_ENABLED $rule]\t$default_severity($rule)\t$default_enabled($rule)"
}
close $properties
if {!$fully_routed || $route_errors} { error "Checkpoint is not fully routed without errors" }
if {[llength [get_drc_violations -quiet -filter {SEVERITY == Error}]] != 0} {
    error "Checkpoint has DRC errors"
}
write_debug_probes -no_partial_ltxfile -force $out/debug_probes.ltx
close_design
puts "CORAL_STAGE_VALIDATION_REPORTS_PASSED"
exit

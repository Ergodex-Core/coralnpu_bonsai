if {$argc != 2} { error "Expected checkpoint and report directory" }
set out [lindex $argv 1]
file mkdir $out
open_checkpoint [lindex $argv 0]
if {![report_route_status -boolean_check ROUTED_FULLY] || [report_route_status -boolean_check ERRORS_IN_ROUTES]} {
    error "Routing incomplete or contains errors"
}
foreach {name period} {npu_clk 20.0 clk_main_a0 4.0 clk_out1_cl_hbm_mmcm 3.333333} {
    set clock [get_clocks -quiet $name]
    if {[llength $clock] != 1 || abs([get_property PERIOD $clock]-$period) > 0.002} {
        error "Unexpected hardware clock $name; expected period $period ns"
    }
}
report_route_status -file $out/route_status.rpt
report_timing_summary -report_unconstrained -file $out/timing_summary.rpt
report_clocks -file $out/clocks.rpt
report_clock_interaction -file $out/clock_interaction.rpt
report_cdc -file $out/cdc.rpt
report_cdc -details -file $out/cdc_details.rpt
report_bus_skew -file $out/bus_skew.rpt
check_timing -verbose -file $out/check_timing.rpt
report_drc -file $out/drc.rpt
if {[llength [get_drc_violations -quiet -filter {SEVERITY == Error}]]} { error "DRC errors" }
proc contents {path} { set f [open $path r]; set s [read $f]; close $f; return $s }
set timing [contents $out/timing_summary.rpt]
if {[string first "All user specified timing constraints are met." $timing] < 0} { error "Timing failed" }
set skew [contents $out/bus_skew.rpt]
if {[string first "VIOLATED" $skew] >= 0 || [string first "Slack (MET)" $skew] < 0} { error "Bus skew failed or not checked" }
set checks [contents $out/check_timing.rpt]
if {![regexp {checking unconstrained_internal_endpoints \(([0-9]+)\)} $checks _ count] || $count != 0} {
    error "Unconstrained internal endpoints"
}
# The pinned AWS shell has protected CDC findings. Reject critical findings
# on other source clocks; preserve the complete report for review on HDK updates.
foreach line [split [contents $out/cdc.rpt] "\n"] {
    if {[regexp {^Critical\s+(\S+)} $line _ source]} {
        if {$source ne "clk_core" && $source ne "WRAPPER/RL/RL_DEBUG_BRIDGE/inst/axi_jtag/inst/u_jtag_proc/tck_i_reg/Q"} {
            error "New critical clock crossing: $line"
        }
    }
}
write_debug_probes -no_partial_ltxfile -force $out/debug_probes.ltx
puts "CI_VALIDATION_PASS"
close_design
exit

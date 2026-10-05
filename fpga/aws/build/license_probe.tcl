# Exercise the synthesis feature for the actual part. This does not accept a
# license, create credentials, or run a customer FPGA implementation.
if {$argc != 1} { error "Expected probe output directory" }
set out [lindex $argv 0]
file mkdir $out
set f [open $out/license_probe.v w]
puts $f {module license_probe(input clk, input d, output reg q); always @(posedge clk) q <= d; endmodule}
close $f
read_verilog $out/license_probe.v
synth_design -mode out_of_context -top license_probe -part xcvu47p-fsvh2892-2-e
puts "CORAL_STAGE_LICENSE_PASSED"
exit

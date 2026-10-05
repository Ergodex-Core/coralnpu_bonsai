# Reset assertion is asynchronous; release is synchronized in each clock domain.
set_false_path -to [get_pins -hier -filter {NAME =~ *reset_sync_reg*/CLR}]
# BUFGCE_DIV generated clock and AXI clock-converter CDC constraints are inferred.

# The shell main clock and HBM reference are independent clock domains.
# AWS SmartConnect and HBM status synchronizers implement their crossings.
set_clock_groups -asynchronous \
  -group [get_clocks -include_generated_clocks [get_clocks -of_objects [get_ports clk_main_a0]]] \
  -group [get_clocks -include_generated_clocks [get_clocks -of_objects [get_ports clk_hbm_ref]]]

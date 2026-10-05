# Reset assertion is asynchronous; release is synchronized in each clock domain.
set_false_path -to [get_pins -hier -filter {NAME =~ *reset_sync_reg*/CLR}]
# BUFGCE_DIV generated clock and AXI clock-converter CDC constraints are inferred.

# Physically configure the HBM AXI MMCM for 300 MHz from its 100 MHz input.
# This changes hardware divider attributes; it does not relax clock constraints.
# VCO = 100 * 24 / 2 = 1200 MHz. Output = 1200 / 4 = 300 MHz.
set hbm_mmcm [get_cells -hier -filter {NAME =~ */CL_HBM/*/HBM_MMCM_I/inst/mmcme4_adv_inst}]
if {[llength $hbm_mmcm] != 1} { error "Expected exactly one HBM AXI MMCM" }
set_property CLKFBOUT_MULT_F 24.000 $hbm_mmcm
set_property DIVCLK_DIVIDE 2 $hbm_mmcm
set_property CLKOUT0_DIVIDE_F 4.000 $hbm_mmcm
puts "HBM AXI hardware clock configured for 300 MHz: $hbm_mmcm"

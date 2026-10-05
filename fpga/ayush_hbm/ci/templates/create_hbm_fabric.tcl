set root @@CORAL_ROOT@@
create_project fabric $root/ip/fabric -part xcvu47p-fsvh2892-2-e -force
set_property target_language Verilog [current_project]
create_bd_design coral_hbm_fabric
set sc [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 sc]
set_property -dict [list CONFIG.NUM_SI {2} CONFIG.NUM_MI {1} CONFIG.NUM_CLKS {2}] $sc
foreach {name mode width ids} {NPU Slave 128 6 HOST Slave 512 6 HBM Master 512 16} {
    set p [create_bd_intf_port -mode $mode -vlnv xilinx.com:interface:aximm_rtl:1.0 $name]
    set_property -dict [list CONFIG.PROTOCOL {AXI4} CONFIG.ADDR_WIDTH {64} \
        CONFIG.DATA_WIDTH $width CONFIG.ID_WIDTH $ids CONFIG.HAS_BURST {1} \
        CONFIG.HAS_LOCK {1} CONFIG.HAS_CACHE {1} CONFIG.HAS_PROT {1} CONFIG.HAS_QOS {1} \
        CONFIG.HAS_REGION {0} CONFIG.SUPPORTS_NARROW_BURST {1} \
        CONFIG.MAX_BURST_LENGTH {256} CONFIG.NUM_READ_OUTSTANDING {16} \
        CONFIG.NUM_WRITE_OUTSTANDING {16}] $p
}
set c0 [create_bd_port -dir I -type clk npu_clk]
set c1 [create_bd_port -dir I -type clk host_clk]
set rst [create_bd_port -dir I -type rst resetn]
set_property -dict [list CONFIG.FREQ_HZ {50000000} CONFIG.ASSOCIATED_BUSIF {NPU} CONFIG.ASSOCIATED_RESET {resetn}] $c0
set_property -dict [list CONFIG.FREQ_HZ {250000000} CONFIG.ASSOCIATED_BUSIF {HOST:HBM} CONFIG.ASSOCIATED_RESET {resetn}] $c1
set_property CONFIG.POLARITY ACTIVE_LOW $rst
connect_bd_net $c0 [get_bd_pins sc/aclk]
connect_bd_net $c1 [get_bd_pins sc/aclk1]
connect_bd_net $rst [get_bd_pins sc/aresetn]
connect_bd_intf_net [get_bd_intf_ports NPU] [get_bd_intf_pins sc/S00_AXI]
connect_bd_intf_net [get_bd_intf_ports HOST] [get_bd_intf_pins sc/S01_AXI]
connect_bd_intf_net [get_bd_intf_ports HBM] [get_bd_intf_pins sc/M00_AXI]
assign_bd_address -offset 0 -range 16G -target_address_space [get_bd_addr_spaces NPU] [get_bd_addr_segs HBM/Reg] -force
assign_bd_address -offset 0 -range 16G -target_address_space [get_bd_addr_spaces HOST] [get_bd_addr_segs HBM/Reg] -force
validate_bd_design
save_bd_design
generate_target all [get_files coral_hbm_fabric.bd]
make_wrapper -files [get_files coral_hbm_fabric.bd] -top
puts "HBM_FABRIC_GENERATION_COMPLETE"
close_project
exit

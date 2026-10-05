set root [file dirname [file normalize [info script]]]
create_project fabric_sim $root/ip/fabric_sim -part xcvu47p-fsvh2892-2-e -force
add_files @@HDK_ROOT@@/hdk/common/ip/cl_ip/cl_ip.srcs/sources_1/bd/cl_axi_sc_1x1/cl_axi_sc_1x1.bd
add_files @@HDK_ROOT@@/hdk/common/ip/cl_ip/cl_ip.gen/sources_1/bd/cl_axi_sc_1x1/hdl/cl_axi_sc_1x1_wrapper.v
set_property XPM_LIBRARIES {XPM_CDC XPM_MEMORY XPM_FIFO} [current_project]
add_files $root/ip/fabric/fabric.srcs/sources_1/bd/coral_hbm_fabric/coral_hbm_fabric.bd
add_files $root/ip/fabric/fabric.gen/sources_1/bd/coral_hbm_fabric/hdl/coral_hbm_fabric_wrapper.v
add_files -fileset sim_1 [list $root/cl_coralnpu_hbm/design/cl_dram_dma_pkg.sv $root/tests/fabric_memory_model.sv $root/tests/tb_fabric.sv]
set_property top tb_fabric [get_filesets sim_1]
set_property xsim.simulate.runtime 0ns [get_filesets sim_1]
update_compile_order -fileset sim_1
launch_simulation
run all
close_sim
close_project
exit

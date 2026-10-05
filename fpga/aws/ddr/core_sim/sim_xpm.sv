// Simulation-only adapter: generic core SRAM requires SYNTHESIS, while the
// functional FIFO deliberately refuses synthesis. Never add to an FPGA filelist.
`undef SYNTHESIS
`include "xpm_fifo_async_model.sv"
`define SYNTHESIS

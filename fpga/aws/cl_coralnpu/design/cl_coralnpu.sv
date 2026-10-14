// ============================================================================
// Amazon FPGA Hardware Development Kit
//
// Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
//
// Licensed under the Amazon Software License (the "License"). You may not use
// this file except in compliance with the License. A copy of the License is
// located at
//
//    http://aws.amazon.com/asl/
//
// or in the "license" file accompanying this file. This file is distributed on
// an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or
// implied. See the License for the specific language governing permissions and
// limitations under the License.
// ============================================================================


//====================================================================================
// Top level module file for cl_coralnpu
//====================================================================================

module cl_coralnpu #(
    parameter EN_DDR = 0,
    parameter EN_HBM = 0
) (
    `include "cl_ports.vh"
);

  `include "cl_id_defines.vh"  // CL ID defines required for all examples
  `include "cl_coralnpu_defines.vh"


  //=============================================================================
  // GLOBALS
  //=============================================================================

  always_comb begin
    cl_sh_flr_done    = 'b1;
    cl_sh_status0     = 'b0;
    cl_sh_status1     = 'b0;
    cl_sh_status2     = 'b0;
    cl_sh_id0         = `CL_SH_ID0;
    cl_sh_id1         = `CL_SH_ID1;
    cl_sh_status_vled = 'b0;
    cl_sh_dma_wr_full = 'b0;
    cl_sh_dma_rd_full = 'b0;
  end


  //=============================================================================
  // PCIM
  //=============================================================================

  // Cause Protocol Violations
  always_comb begin
    cl_sh_pcim_awaddr  = 'b0;
    cl_sh_pcim_awsize  = 'b0;
    cl_sh_pcim_awburst = 'b0;
    cl_sh_pcim_awvalid = 'b0;

    cl_sh_pcim_wdata   = 'b0;
    cl_sh_pcim_wstrb   = 'b0;
    cl_sh_pcim_wlast   = 'b0;
    cl_sh_pcim_wvalid  = 'b0;

    cl_sh_pcim_araddr  = 'b0;
    cl_sh_pcim_arsize  = 'b0;
    cl_sh_pcim_arburst = 'b0;
    cl_sh_pcim_arvalid = 'b0;
  end

  // Remaining CL Output Ports
  always_comb begin
    cl_sh_pcim_awid    = 'b0;
    cl_sh_pcim_awlen   = 'b0;
    cl_sh_pcim_awcache = 'b0;
    cl_sh_pcim_awlock  = 'b0;
    cl_sh_pcim_awprot  = 'b0;
    cl_sh_pcim_awqos   = 'b0;
    cl_sh_pcim_awuser  = 'b0;

    cl_sh_pcim_wid     = 'b0;
    cl_sh_pcim_wuser   = 'b0;

    cl_sh_pcim_arid    = 'b0;
    cl_sh_pcim_arlen   = 'b0;
    cl_sh_pcim_arcache = 'b0;
    cl_sh_pcim_arlock  = 'b0;
    cl_sh_pcim_arprot  = 'b0;
    cl_sh_pcim_arqos   = 'b0;
    cl_sh_pcim_aruser  = 'b0;

    cl_sh_pcim_rready  = 'b0;
    cl_sh_pcim_bready  = 'b0;
  end

  //=============================================================================
  // PCIS
  //=============================================================================

  // Cause Protocol Violations
  always_comb begin
    cl_sh_dma_pcis_bresp  = 'b0;
    cl_sh_dma_pcis_rresp  = 'b0;
    cl_sh_dma_pcis_rvalid = 'b0;
  end

  // Remaining CL Output Ports
  always_comb begin
    cl_sh_dma_pcis_awready = 'b0;

    cl_sh_dma_pcis_wready  = 'b0;

    cl_sh_dma_pcis_bid     = 'b0;
    cl_sh_dma_pcis_bvalid  = 'b0;

    cl_sh_dma_pcis_arready = 'b0;

    cl_sh_dma_pcis_rid     = 'b0;
    cl_sh_dma_pcis_rdata   = 'b0;
    cl_sh_dma_pcis_rlast   = 'b0;
    cl_sh_dma_pcis_ruser   = 'b0;
  end

  //=============================================================================
  // OCL
  //=============================================================================

  wire npu_clk;
  wire npu_rst_n;
  (* ASYNC_REG="TRUE" *) logic [2:0] npu_reset_sync;
  (* ASYNC_REG="TRUE" *) logic [2:0] host_reset_sync;
  wire host_rst_n = host_reset_sync[2];
  BUFGCE_DIV #(
      .BUFGCE_DIVIDE(5),
      .SIM_DEVICE("ULTRASCALE_PLUS")
  ) i_npu_clk (
      .I  (clk_main_a0),
      .CE (1'b1),
      .CLR(1'b0),
      .O  (npu_clk)
  );
  always_ff @(posedge clk_main_a0 or negedge rst_main_n)
    if (!rst_main_n) host_reset_sync <= '0;
    else host_reset_sync <= {host_reset_sync[1:0], 1'b1};
  always_ff @(posedge npu_clk or negedge rst_main_n)
    if (!rst_main_n) npu_reset_sync <= '0;
    else npu_reset_sync <= {npu_reset_sync[1:0], 1'b1};
  assign npu_rst_n = npu_reset_sync[2];
  wire [31:0] n_awaddr;
  wire n_awvalid;
  wire n_awready;
  wire [31:0] n_wdata;
  wire [3:0] n_wstrb;
  wire n_wvalid;
  wire n_wready;
  wire [1:0] n_bresp;
  wire n_bvalid;
  wire n_bready;
  wire [31:0] n_araddr;
  wire n_arvalid;
  wire n_arready;
  wire [31:0] n_rdata;
  wire [1:0] n_rresp;
  wire n_rvalid;
  wire n_rready;
  cl_axi_clock_converter_light i_host_cdc (
      .s_axi_aclk(clk_main_a0),
      .s_axi_aresetn(host_rst_n),
      .s_axi_awprot(3'b0),
      .s_axi_arprot(3'b0),
      .m_axi_aclk(npu_clk),
      .m_axi_aresetn(npu_rst_n),
      .m_axi_awprot(),
      .m_axi_arprot(),
      .s_axi_awaddr(ocl_cl_awaddr),
      .m_axi_awaddr(n_awaddr),
      .s_axi_awvalid(ocl_cl_awvalid),
      .m_axi_awvalid(n_awvalid),
      .s_axi_awready(cl_ocl_awready),
      .m_axi_awready(n_awready),
      .s_axi_wdata(ocl_cl_wdata),
      .m_axi_wdata(n_wdata),
      .s_axi_wstrb(ocl_cl_wstrb),
      .m_axi_wstrb(n_wstrb),
      .s_axi_wvalid(ocl_cl_wvalid),
      .m_axi_wvalid(n_wvalid),
      .s_axi_wready(cl_ocl_wready),
      .m_axi_wready(n_wready),
      .s_axi_bresp(cl_ocl_bresp),
      .m_axi_bresp(n_bresp),
      .s_axi_bvalid(cl_ocl_bvalid),
      .m_axi_bvalid(n_bvalid),
      .s_axi_bready(ocl_cl_bready),
      .m_axi_bready(n_bready),
      .s_axi_araddr(ocl_cl_araddr),
      .m_axi_araddr(n_araddr),
      .s_axi_arvalid(ocl_cl_arvalid),
      .m_axi_arvalid(n_arvalid),
      .s_axi_arready(cl_ocl_arready),
      .m_axi_arready(n_arready),
      .s_axi_rdata(cl_ocl_rdata),
      .m_axi_rdata(n_rdata),
      .s_axi_rresp(cl_ocl_rresp),
      .m_axi_rresp(n_rresp),
      .s_axi_rvalid(cl_ocl_rvalid),
      .m_axi_rvalid(n_rvalid),
      .s_axi_rready(ocl_cl_rready),
      .m_axi_rready(n_rready)
  );
  coral_host i_npu (
      .clk(npu_clk),
      .rst_n(npu_rst_n),
      .s_awaddr(n_awaddr),
      .s_awvalid(n_awvalid),
      .s_awready(n_awready),
      .s_wdata(n_wdata),
      .s_wstrb(n_wstrb),
      .s_wvalid(n_wvalid),
      .s_wready(n_wready),
      .s_bresp(n_bresp),
      .s_bvalid(n_bvalid),
      .s_bready(n_bready),
      .s_araddr(n_araddr),
      .s_arvalid(n_arvalid),
      .s_arready(n_arready),
      .s_rdata(n_rdata),
      .s_rresp(n_rresp),
      .s_rvalid(n_rvalid),
      .s_rready(n_rready)
  );

  //=============================================================================
  // SDA
  //=============================================================================

  // Cause Protocol Violations
  always_comb begin
    cl_sda_bresp  = 'b0;
    cl_sda_rresp  = 'b0;
    cl_sda_rvalid = 'b0;
  end

  // Remaining CL Output Ports
  always_comb begin
    cl_sda_awready = 'b0;
    cl_sda_wready  = 'b0;

    cl_sda_bvalid  = 'b0;

    cl_sda_arready = 'b0;

    cl_sda_rdata   = 'b0;
  end

  //=============================================================================
  // SH_DDR
  //=============================================================================

  sh_ddr #(
      .DDR_PRESENT(EN_DDR)
  ) SH_DDR (
      .clk                  (clk_main_a0),
      .rst_n                (),
      .stat_clk             (clk_main_a0),
      .stat_rst_n           (),
      .CLK_DIMM_DP          (CLK_DIMM_DP),
      .CLK_DIMM_DN          (CLK_DIMM_DN),
      .M_ACT_N              (M_ACT_N),
      .M_MA                 (M_MA),
      .M_BA                 (M_BA),
      .M_BG                 (M_BG),
      .M_CKE                (M_CKE),
      .M_ODT                (M_ODT),
      .M_CS_N               (M_CS_N),
      .M_CLK_DN             (M_CLK_DN),
      .M_CLK_DP             (M_CLK_DP),
      .M_PAR                (M_PAR),
      .M_DQ                 (M_DQ),
      .M_ECC                (M_ECC),
      .M_DQS_DP             (M_DQS_DP),
      .M_DQS_DN             (M_DQS_DN),
      .cl_RST_DIMM_N        (RST_DIMM_N),
      .cl_sh_ddr_axi_awid   (),
      .cl_sh_ddr_axi_awaddr (),
      .cl_sh_ddr_axi_awlen  (),
      .cl_sh_ddr_axi_awsize (),
      .cl_sh_ddr_axi_awvalid(),
      .cl_sh_ddr_axi_awburst(),
      .cl_sh_ddr_axi_awuser (),
      .cl_sh_ddr_axi_awready(),
      .cl_sh_ddr_axi_wdata  (),
      .cl_sh_ddr_axi_wstrb  (),
      .cl_sh_ddr_axi_wlast  (),
      .cl_sh_ddr_axi_wvalid (),
      .cl_sh_ddr_axi_wready (),
      .cl_sh_ddr_axi_bid    (),
      .cl_sh_ddr_axi_bresp  (),
      .cl_sh_ddr_axi_bvalid (),
      .cl_sh_ddr_axi_bready (),
      .cl_sh_ddr_axi_arid   (),
      .cl_sh_ddr_axi_araddr (),
      .cl_sh_ddr_axi_arlen  (),
      .cl_sh_ddr_axi_arsize (),
      .cl_sh_ddr_axi_arvalid(),
      .cl_sh_ddr_axi_arburst(),
      .cl_sh_ddr_axi_aruser (),
      .cl_sh_ddr_axi_arready(),
      .cl_sh_ddr_axi_rid    (),
      .cl_sh_ddr_axi_rdata  (),
      .cl_sh_ddr_axi_rresp  (),
      .cl_sh_ddr_axi_rlast  (),
      .cl_sh_ddr_axi_rvalid (),
      .cl_sh_ddr_axi_rready (),
      .sh_ddr_stat_bus_addr (),
      .sh_ddr_stat_bus_wdata(),
      .sh_ddr_stat_bus_wr   (),
      .sh_ddr_stat_bus_rd   (),
      .sh_ddr_stat_bus_ack  (),
      .sh_ddr_stat_bus_rdata(),
      .ddr_sh_stat_int      (),
      .sh_cl_ddr_is_ready   ()
  );

  always_comb begin
    cl_sh_ddr_stat_ack   = 'b0;
    cl_sh_ddr_stat_rdata = 'b0;
    cl_sh_ddr_stat_int   = 'b0;
  end

  //=============================================================================
  // USER-DEFIEND INTERRUPTS
  //=============================================================================

  always_comb begin
    cl_sh_apppf_irq_req = 'b0;
  end

  //=============================================================================
  // VIRTUAL JTAG
  //=============================================================================

  always_comb begin
    tdo = 'b0;
  end

  //=============================================================================
  // HBM MONITOR IO
  //=============================================================================

  always_comb begin
    hbm_apb_paddr_1   = 'b0;
    hbm_apb_pprot_1   = 'b0;
    hbm_apb_psel_1    = 'b0;
    hbm_apb_penable_1 = 'b0;
    hbm_apb_pwrite_1  = 'b0;
    hbm_apb_pwdata_1  = 'b0;
    hbm_apb_pstrb_1   = 'b0;
    hbm_apb_pready_1  = 'b0;
    hbm_apb_prdata_1  = 'b0;
    hbm_apb_pslverr_1 = 'b0;

    hbm_apb_paddr_0   = 'b0;
    hbm_apb_pprot_0   = 'b0;
    hbm_apb_psel_0    = 'b0;
    hbm_apb_penable_0 = 'b0;
    hbm_apb_pwrite_0  = 'b0;
    hbm_apb_pwdata_0  = 'b0;
    hbm_apb_pstrb_0   = 'b0;
    hbm_apb_pready_0  = 'b0;
    hbm_apb_prdata_0  = 'b0;
    hbm_apb_pslverr_0 = 'b0;
  end

  //=============================================================================
  //
  //=============================================================================

  always_comb begin
    PCIE_EP_TXP    = 'b0;
    PCIE_EP_TXN    = 'b0;

    PCIE_RP_PERSTN = 'b0;
    PCIE_RP_TXP    = 'b0;
    PCIE_RP_TXN    = 'b0;
  end

endmodule  // cl_coralnpu

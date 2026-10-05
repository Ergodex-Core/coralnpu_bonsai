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
    parameter EN_DDR = 1,
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

  assign cl_sh_dma_pcis_ruser = 64'b0;

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
  wire [31:0] c_awaddr;
  wire [5:0] c_awid;
  wire [7:0] c_awlen;
  wire [2:0] c_awsize;
  wire [1:0] c_awburst;
  wire c_awlock;
  wire c_awvalid;
  wire c_awready;
  wire [127:0] c_wdata;
  wire [15:0] c_wstrb;
  wire c_wlast;
  wire c_wvalid;
  wire c_wready;
  wire [5:0] c_bid;
  wire [1:0] c_bresp;
  wire c_bvalid;
  wire c_bready;
  wire [31:0] c_araddr;
  wire [5:0] c_arid;
  wire [7:0] c_arlen;
  wire [2:0] c_arsize;
  wire [1:0] c_arburst;
  wire c_arlock;
  wire c_arvalid;
  wire c_arready;
  wire [127:0] c_rdata;
  wire [5:0] c_rid;
  wire [1:0] c_rresp;
  wire c_rlast;
  wire c_rvalid;
  wire c_rready;
  wire [63:0] d_awaddr;
  wire [15:0] d_awid;
  wire [7:0] d_awlen;
  wire [2:0] d_awsize;
  wire [1:0] d_awburst;
  wire d_awvalid;
  wire d_awready;
  wire [511:0] d_wdata;
  wire [63:0] d_wstrb;
  wire d_wlast;
  wire d_wvalid;
  wire d_wready;
  wire [15:0] d_bid;
  wire [1:0] d_bresp;
  wire d_bvalid;
  wire d_bready;
  wire [63:0] d_araddr;
  wire [15:0] d_arid;
  wire [7:0] d_arlen;
  wire [2:0] d_arsize;
  wire [1:0] d_arburst;
  wire d_arvalid;
  wire d_arready;
  wire [511:0] d_rdata;
  wire [15:0] d_rid;
  wire [1:0] d_rresp;
  wire d_rlast;
  wire d_rvalid;
  wire d_rready;
  wire ddr_ready, ddr_fault;
  (* ASYNC_REG="TRUE" *) logic [2:0] ddr_ready_sync, ddr_fault_sync;
  always_ff @(posedge npu_clk or negedge rst_main_n)
    if (!rst_main_n) begin
      ddr_ready_sync <= 0;
      ddr_fault_sync <= 0;
    end else begin
      ddr_ready_sync <= {ddr_ready_sync[1:0], ddr_ready};
      ddr_fault_sync <= {ddr_fault_sync[1:0], ddr_fault};
    end
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
      .s_rready(n_rready),
      .m_awaddr(c_awaddr),
      .m_awid(c_awid),
      .m_awlen(c_awlen),
      .m_awsize(c_awsize),
      .m_awburst(c_awburst),
      .m_awlock(c_awlock),
      .m_awvalid(c_awvalid),
      .m_awready(c_awready),
      .m_wdata(c_wdata),
      .m_wstrb(c_wstrb),
      .m_wlast(c_wlast),
      .m_wvalid(c_wvalid),
      .m_wready(c_wready),
      .m_bid(c_bid),
      .m_bresp(c_bresp),
      .m_bvalid(c_bvalid),
      .m_bready(c_bready),
      .m_araddr(c_araddr),
      .m_arid(c_arid),
      .m_arlen(c_arlen),
      .m_arsize(c_arsize),
      .m_arburst(c_arburst),
      .m_arlock(c_arlock),
      .m_arvalid(c_arvalid),
      .m_arready(c_arready),
      .m_rdata(c_rdata),
      .m_rid(c_rid),
      .m_rresp(c_rresp),
      .m_rlast(c_rlast),
      .m_rvalid(c_rvalid),
      .m_rready(c_rready),
      .ddr_ready(ddr_ready_sync[2]),
      .ddr_present(EN_DDR != 0),
      .ddr_fault(ddr_fault_sync[2])
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

  coral_ddr_subsystem i_ddr_path (
      .core_clk(npu_clk),
      .core_rst_n(npu_rst_n),
      .host_clk(clk_main_a0),
      .host_rst_n(host_rst_n),
      .ddr_ready(ddr_ready && (EN_DDR != 0)),
      .ddr_fault(ddr_fault),
      .c_awaddr(c_awaddr),
      .c_awid(c_awid),
      .c_awlen(c_awlen),
      .c_awsize(c_awsize),
      .c_awburst(c_awburst),
      .c_awlock(c_awlock),
      .c_awvalid(c_awvalid),
      .c_awready(c_awready),
      .c_wdata(c_wdata),
      .c_wstrb(c_wstrb),
      .c_wlast(c_wlast),
      .c_wvalid(c_wvalid),
      .c_wready(c_wready),
      .c_bid(c_bid),
      .c_bresp(c_bresp),
      .c_bvalid(c_bvalid),
      .c_bready(c_bready),
      .c_araddr(c_araddr),
      .c_arid(c_arid),
      .c_arlen(c_arlen),
      .c_arsize(c_arsize),
      .c_arburst(c_arburst),
      .c_arlock(c_arlock),
      .c_arvalid(c_arvalid),
      .c_arready(c_arready),
      .c_rdata(c_rdata),
      .c_rid(c_rid),
      .c_rresp(c_rresp),
      .c_rlast(c_rlast),
      .c_rvalid(c_rvalid),
      .c_rready(c_rready),
      .h_awaddr(sh_cl_dma_pcis_awaddr),
      .h_awid(sh_cl_dma_pcis_awid),
      .h_awlen(sh_cl_dma_pcis_awlen),
      .h_awsize(sh_cl_dma_pcis_awsize),
      .h_awburst(sh_cl_dma_pcis_awburst),
      .h_awlock(sh_cl_dma_pcis_awlock),
      .h_awvalid(sh_cl_dma_pcis_awvalid),
      .h_awready(cl_sh_dma_pcis_awready),
      .h_wdata(sh_cl_dma_pcis_wdata),
      .h_wstrb(sh_cl_dma_pcis_wstrb),
      .h_wlast(sh_cl_dma_pcis_wlast),
      .h_wvalid(sh_cl_dma_pcis_wvalid),
      .h_wready(cl_sh_dma_pcis_wready),
      .h_bid(cl_sh_dma_pcis_bid),
      .h_bresp(cl_sh_dma_pcis_bresp),
      .h_bvalid(cl_sh_dma_pcis_bvalid),
      .h_bready(sh_cl_dma_pcis_bready),
      .h_araddr(sh_cl_dma_pcis_araddr),
      .h_arid(sh_cl_dma_pcis_arid),
      .h_arlen(sh_cl_dma_pcis_arlen),
      .h_arsize(sh_cl_dma_pcis_arsize),
      .h_arburst(sh_cl_dma_pcis_arburst),
      .h_arlock(sh_cl_dma_pcis_arlock),
      .h_arvalid(sh_cl_dma_pcis_arvalid),
      .h_arready(cl_sh_dma_pcis_arready),
      .h_rdata(cl_sh_dma_pcis_rdata),
      .h_rid(cl_sh_dma_pcis_rid),
      .h_rresp(cl_sh_dma_pcis_rresp),
      .h_rlast(cl_sh_dma_pcis_rlast),
      .h_rvalid(cl_sh_dma_pcis_rvalid),
      .h_rready(sh_cl_dma_pcis_rready),
      .m_awaddr(d_awaddr),
      .m_awid(d_awid),
      .m_awlen(d_awlen),
      .m_awsize(d_awsize),
      .m_awburst(d_awburst),
      .m_awvalid(d_awvalid),
      .m_awready(d_awready),
      .m_wdata(d_wdata),
      .m_wstrb(d_wstrb),
      .m_wlast(d_wlast),
      .m_wvalid(d_wvalid),
      .m_wready(d_wready),
      .m_bid(d_bid),
      .m_bresp(d_bresp),
      .m_bvalid(d_bvalid),
      .m_bready(d_bready),
      .m_araddr(d_araddr),
      .m_arid(d_arid),
      .m_arlen(d_arlen),
      .m_arsize(d_arsize),
      .m_arburst(d_arburst),
      .m_arvalid(d_arvalid),
      .m_arready(d_arready),
      .m_rdata(d_rdata),
      .m_rid(d_rid),
      .m_rresp(d_rresp),
      .m_rlast(d_rlast),
      .m_rvalid(d_rvalid),
      .m_rready(d_rready)
  );

  sh_ddr #(
      .DDR_PRESENT(EN_DDR)
  ) SH_DDR (
      .clk                  (clk_main_a0),
      .rst_n                (host_rst_n),
      .stat_clk             (clk_main_a0),
      .stat_rst_n           (host_rst_n),
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
      .cl_sh_ddr_axi_awid   (d_awid),
      .cl_sh_ddr_axi_awaddr (d_awaddr),
      .cl_sh_ddr_axi_awlen  (d_awlen),
      .cl_sh_ddr_axi_awsize (d_awsize),
      .cl_sh_ddr_axi_awvalid(d_awvalid),
      .cl_sh_ddr_axi_awburst(d_awburst),
      .cl_sh_ddr_axi_awuser (1'b0),
      .cl_sh_ddr_axi_awready(d_awready),
      .cl_sh_ddr_axi_wdata  (d_wdata),
      .cl_sh_ddr_axi_wstrb  (d_wstrb),
      .cl_sh_ddr_axi_wlast  (d_wlast),
      .cl_sh_ddr_axi_wvalid (d_wvalid),
      .cl_sh_ddr_axi_wready (d_wready),
      .cl_sh_ddr_axi_bid    (d_bid),
      .cl_sh_ddr_axi_bresp  (d_bresp),
      .cl_sh_ddr_axi_bvalid (d_bvalid),
      .cl_sh_ddr_axi_bready (d_bready),
      .cl_sh_ddr_axi_arid   (d_arid),
      .cl_sh_ddr_axi_araddr (d_araddr),
      .cl_sh_ddr_axi_arlen  (d_arlen),
      .cl_sh_ddr_axi_arsize (d_arsize),
      .cl_sh_ddr_axi_arvalid(d_arvalid),
      .cl_sh_ddr_axi_arburst(d_arburst),
      .cl_sh_ddr_axi_aruser (1'b0),
      .cl_sh_ddr_axi_arready(d_arready),
      .cl_sh_ddr_axi_rid    (d_rid),
      .cl_sh_ddr_axi_rdata  (d_rdata),
      .cl_sh_ddr_axi_rresp  (d_rresp),
      .cl_sh_ddr_axi_rlast  (d_rlast),
      .cl_sh_ddr_axi_rvalid (d_rvalid),
      .cl_sh_ddr_axi_rready (d_rready),
      .sh_ddr_stat_bus_addr (sh_cl_ddr_stat_addr),
      .sh_ddr_stat_bus_wdata(sh_cl_ddr_stat_wdata),
      .sh_ddr_stat_bus_wr   (sh_cl_ddr_stat_wr),
      .sh_ddr_stat_bus_rd   (sh_cl_ddr_stat_rd),
      .sh_ddr_stat_bus_ack  (cl_sh_ddr_stat_ack),
      .sh_ddr_stat_bus_rdata(cl_sh_ddr_stat_rdata),
      .ddr_sh_stat_int      (cl_sh_ddr_stat_int),
      .sh_cl_ddr_is_ready   (ddr_ready)
  );


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

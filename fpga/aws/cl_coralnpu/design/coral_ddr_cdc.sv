// Two vendor-supported asynchronous FIFOs cross line requests and responses.
// Shell reset is the sole reset authority for both clock domains. Core CSR
// reset deliberately does not reset these queues or any DDR AXI transaction.
module coral_ddr_cdc (
    input wire s_clk,
    s_rst_n,
    m_clk,
    m_rst_n,
    input wire s_req_valid,
    output wire s_req_ready,
    input wire s_req_write,
    input wire [31:0] s_req_addr,
    input wire [511:0] s_req_wdata,
    input wire [63:0] s_req_wstrb,
    output wire s_rsp_valid,
    input wire s_rsp_ready,
    output wire [511:0] s_rsp_rdata,
    output wire [1:0] s_rsp_resp,
    output wire m_req_valid,
    input wire m_req_ready,
    output wire m_req_write,
    output wire [31:0] m_req_addr,
    output wire [511:0] m_req_wdata,
    output wire [63:0] m_req_wstrb,
    input wire m_rsp_valid,
    output wire m_rsp_ready,
    input wire [511:0] m_rsp_rdata,
    input wire [1:0] m_rsp_resp
);
  // Stretch reset to eight write-clock cycles for each XPM FIFO. Both inputs
  // derive from shell reset; core CSR reset must never flush outstanding AXI.
  logic [7:0] s_release, m_release;
  always_ff @(posedge s_clk or negedge s_rst_n)
    if (!s_rst_n) s_release <= 0;
    else s_release <= {s_release[6:0], 1'b1};
  always_ff @(posedge m_clk or negedge m_rst_n)
    if (!m_rst_n) m_release <= 0;
    else m_release <= {m_release[6:0], 1'b1};
  wire req_full, req_empty, req_wr_busy, req_rd_busy;
  wire rsp_full, rsp_empty, rsp_wr_busy, rsp_rd_busy;
  assign s_req_ready = s_rst_n && !req_full && !req_wr_busy;
  assign m_req_valid = m_rst_n && !req_empty && !req_rd_busy;
  assign m_rsp_ready = m_rst_n && !rsp_full && !rsp_wr_busy;
  assign s_rsp_valid = s_rst_n && !rsp_empty && !rsp_rd_busy;
  xpm_fifo_async #(
      .FIFO_MEMORY_TYPE("distributed"),
      .FIFO_WRITE_DEPTH(16),
      .WRITE_DATA_WIDTH(609),
      .READ_DATA_WIDTH(609),
      .READ_MODE("fwft"),
      .FIFO_READ_LATENCY(0),
      .CDC_SYNC_STAGES(3),
      .WR_DATA_COUNT_WIDTH(5),
      .RD_DATA_COUNT_WIDTH(5),
      .USE_ADV_FEATURES("0000"),
      .SIM_ASSERT_CHK(1)
  ) i_request_fifo (
      .rst(!s_release[7]),
      .wr_clk(s_clk),
      .rd_clk(m_clk),
      .din({s_req_write, s_req_addr, s_req_wdata, s_req_wstrb}),
      .wr_en(s_req_valid && s_req_ready),
      .rd_en(m_req_valid && m_req_ready),
      .dout({m_req_write, m_req_addr, m_req_wdata, m_req_wstrb}),
      .full(req_full),
      .empty(req_empty),
      .wr_rst_busy(req_wr_busy),
      .rd_rst_busy(req_rd_busy),
      .sleep(1'b0),
      .injectsbiterr(1'b0),
      .injectdbiterr(1'b0),
      .almost_empty(),
      .almost_full(),
      .data_valid(),
      .dbiterr(),
      .overflow(),
      .prog_empty(),
      .prog_full(),
      .rd_data_count(),
      .sbiterr(),
      .underflow(),
      .wr_ack(),
      .wr_data_count()
  );
  xpm_fifo_async #(
      .FIFO_MEMORY_TYPE("distributed"),
      .FIFO_WRITE_DEPTH(16),
      .WRITE_DATA_WIDTH(514),
      .READ_DATA_WIDTH(514),
      .READ_MODE("fwft"),
      .FIFO_READ_LATENCY(0),
      .CDC_SYNC_STAGES(3),
      .WR_DATA_COUNT_WIDTH(5),
      .RD_DATA_COUNT_WIDTH(5),
      .USE_ADV_FEATURES("0000"),
      .SIM_ASSERT_CHK(1)
  ) i_response_fifo (
      .rst(!m_release[7]),
      .wr_clk(m_clk),
      .rd_clk(s_clk),
      .din({m_rsp_rdata, m_rsp_resp}),
      .wr_en(m_rsp_valid && m_rsp_ready),
      .rd_en(s_rsp_valid && s_rsp_ready),
      .dout({s_rsp_rdata, s_rsp_resp}),
      .full(rsp_full),
      .empty(rsp_empty),
      .wr_rst_busy(rsp_wr_busy),
      .rd_rst_busy(rsp_rd_busy),
      .sleep(1'b0),
      .injectsbiterr(1'b0),
      .injectdbiterr(1'b0),
      .almost_empty(),
      .almost_full(),
      .data_valid(),
      .dbiterr(),
      .overflow(),
      .prog_empty(),
      .prog_full(),
      .rd_data_count(),
      .sbiterr(),
      .underflow(),
      .wr_ack(),
      .wr_data_count()
  );
endmodule

// Fair, single-outstanding line arbiter and SH_DDR AXI master. A watchdog or
// protocol/calibration fault returns SLVERR once and permanently stops new work
// until shell reset. Already asserted AXI channels remain valid until accepted;
// late responses are drained and cannot be mistaken for a subsequent request.
module coral_ddr_backend #(
    parameter integer TIMEOUT_CYCLES = 1048576
) (
    input wire clk, rst_n, ddr_ready,
    output logic fault,
    input wire [1:0] req_valid,
    output logic [1:0] req_ready,
    input wire [1:0] req_write,
    input wire [1:0][31:0] req_addr,
    input wire [1:0][511:0] req_wdata,
    input wire [1:0][63:0] req_wstrb,
    output logic [1:0] rsp_valid,
    input wire [1:0] rsp_ready,
    output wire [511:0] rsp_rdata,
    output wire [1:0] rsp_resp,
    output wire [15:0] m_awid,
    output wire [63:0] m_awaddr,
    output wire [7:0] m_awlen,
    output wire [2:0] m_awsize,
    output wire [1:0] m_awburst,
    output wire m_awvalid,
    input wire m_awready,
    output wire [511:0] m_wdata,
    output wire [63:0] m_wstrb,
    output wire m_wlast, m_wvalid,
    input wire m_wready,
    input wire [15:0] m_bid,
    input wire [1:0] m_bresp,
    input wire m_bvalid,
    output wire m_bready,
    output wire [15:0] m_arid,
    output wire [63:0] m_araddr,
    output wire [7:0] m_arlen,
    output wire [2:0] m_arsize,
    output wire [1:0] m_arburst,
    output wire m_arvalid,
    input wire m_arready,
    input wire [15:0] m_rid,
    input wire [511:0] m_rdata,
    input wire [1:0] m_rresp,
    input wire m_rlast, m_rvalid,
    output wire m_rready
);
  localparam [1:0] IDLE=0, ACTIVE=1, RESPOND=2, STOPPED=3;
  logic [1:0] state;
  logic owner_q, last_owner, write_q, inflight, done_q;
  logic aw_pending, w_pending, ar_pending;
  logic [31:0] addr_q;
  logic [511:0] wdata_q, rdata_q;
  logic [63:0] wstrb_q;
  logic [1:0] resp_q;
  logic [31:0] watchdog;
  wire selected = req_valid[0] && req_valid[1] ? !last_owner : req_valid[1];
  wire [31:0] selected_addr = req_addr[selected];
  wire take = |(req_valid & req_ready);
  wire write_response = m_bvalid && m_bready;
  wire read_response = m_rvalid && m_rready;

  always_comb begin
    req_ready = 0;
    rsp_valid = 0;
    if (rst_n && state == IDLE && ddr_ready && !fault)
      req_ready[selected] = 1;
    if (state == RESPOND) rsp_valid[owner_q] = 1;
  end
  assign rsp_rdata = rdata_q;
  assign rsp_resp = resp_q;
  assign m_awid = 0;
  assign m_awaddr = {32'b0,addr_q};
  assign m_awlen = 0;
  assign m_awsize = 6;
  assign m_awburst = 1;
  assign m_awvalid = aw_pending;
  assign m_wdata = wdata_q;
  assign m_wstrb = wstrb_q;
  assign m_wlast = 1;
  assign m_wvalid = w_pending;
  assign m_bready = inflight && write_q && !done_q && !aw_pending && !w_pending;
  assign m_arid = 0;
  assign m_araddr = {32'b0,addr_q};
  assign m_arlen = 0;
  assign m_arsize = 6;
  assign m_arburst = 1;
  assign m_arvalid = ar_pending;
  assign m_rready = inflight && !write_q && !done_q && !ar_pending;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE;
      owner_q <= 0;
      last_owner <= 1;
      write_q <= 0;
      inflight <= 0;
      done_q <= 0;
      aw_pending <= 0;
      w_pending <= 0;
      ar_pending <= 0;
      addr_q <= 0;
      wdata_q <= 0;
      wstrb_q <= 0;
      rdata_q <= 0;
      resp_q <= 0;
      watchdog <= 0;
      fault <= 0;
    end else begin
      if (m_awvalid && m_awready) aw_pending <= 0;
      if (m_wvalid && m_wready) w_pending <= 0;
      if (m_arvalid && m_arready) ar_pending <= 0;
      if (write_response || (read_response && m_rlast)) done_q <= 1;
      case (state)
        IDLE: if (take) begin
          owner_q <= selected;
          last_owner <= selected;
          write_q <= req_write[selected];
          addr_q <= req_addr[selected];
          wdata_q <= req_wdata[selected];
          wstrb_q <= req_wstrb[selected];
          rdata_q <= 0;
          resp_q <= 0;
          watchdog <= 0;
          done_q <= 0;
          if (selected_addr[31] || selected_addr[5:0] != 0) begin
            resp_q <= 2'b11;
            inflight <= 0;
            state <= RESPOND;
          end else begin
            aw_pending <= req_write[selected];
            w_pending <= req_write[selected];
            ar_pending <= !req_write[selected];
            inflight <= 1;
            state <= ACTIVE;
          end
        end
        ACTIVE: begin
          watchdog <= watchdog + 1'b1;
          if (!ddr_ready || watchdog >= TIMEOUT_CYCLES-1) begin
            fault <= 1;
            resp_q <= 2'b10;
            rdata_q <= 0;
            state <= RESPOND;
          end else if (write_response) begin
            resp_q <= m_bid == 0 ? m_bresp : 2'b10;
            if (m_bid != 0) fault <= 1;
            state <= RESPOND;
          end else if (read_response) begin
            rdata_q <= m_rdata;
            resp_q <= (m_rid == 0 && m_rlast) ? m_rresp : 2'b10;
            if (m_rid != 0 || !m_rlast) fault <= 1;
            state <= RESPOND;
          end
        end
        RESPOND: if (rsp_valid[owner_q] && rsp_ready[owner_q]) begin
          if (fault) state <= STOPPED;
          else begin
            inflight <= 0;
            state <= IDLE;
          end
        end
        STOPPED: begin end
        default: state <= STOPPED;
      endcase
    end
  end
endmodule

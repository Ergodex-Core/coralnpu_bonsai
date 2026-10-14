module coral_host (
    input logic clk,
    rst_n,
    input wire [31:0] s_awaddr,
    input wire s_awvalid,
    output wire s_awready,
    input wire [31:0] s_wdata,
    input wire [3:0] s_wstrb,
    input wire s_wvalid,
    output wire s_wready,
    output wire [1:0] s_bresp,
    output wire s_bvalid,
    input wire s_bready,
    input wire [31:0] s_araddr,
    input wire s_arvalid,
    output wire s_arready,
    output wire [31:0] s_rdata,
    output wire [1:0] s_rresp,
    output wire s_rvalid,
    input wire s_rready
);

  // Capture independent AXI-Lite address/data channels before widening WSTRB.
  logic aw_full, w_full, wr_busy, aw_sent, w_sent, rd_busy;
  logic [31:0] aw_q, wd_q;
  logic [3:0] ws_q;
  logic [1:0] read_lane;
  wire n_awready, n_wready, n_arready;
  wire [127:0] n_rdata;
  wire n_awvalid = wr_busy && !aw_sent;
  wire n_wvalid = wr_busy && !w_sent;
  assign s_awready = !aw_full && !wr_busy;
  assign s_wready  = !w_full && !wr_busy;
  assign s_arready = !rd_busy && n_arready;
  assign s_rdata   = n_rdata[read_lane*32+:32];
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      aw_full <= 0;
      w_full <= 0;
      wr_busy <= 0;
      aw_sent <= 0;
      w_sent <= 0;
      rd_busy <= 0;
      aw_q <= 0;
      wd_q <= 0;
      ws_q <= 0;
      read_lane <= 0;
    end else begin
      if (s_awvalid && s_awready) begin
        aw_q <= s_awaddr;
        aw_full <= 1;
      end
      if (s_wvalid && s_wready) begin
        wd_q   <= s_wdata;
        ws_q   <= s_wstrb;
        w_full <= 1;
      end
      if (aw_full && w_full && !wr_busy) begin
        wr_busy <= 1;
        aw_sent <= 0;
        w_sent  <= 0;
      end
      if (n_awvalid && n_awready) aw_sent <= 1;
      if (n_wvalid && n_wready) w_sent <= 1;
      if (s_bvalid && s_bready) begin
        wr_busy <= 0;
        aw_full <= 0;
        w_full  <= 0;
      end
      if (s_arvalid && s_arready) begin
        rd_busy   <= 1;
        read_lane <= s_araddr[3:2];
      end
      if (s_rvalid && s_rready) rd_busy <= 0;
    end
  end

  // This first bring-up exposes TCM/CSRs only. Complete external-memory
  // requests with DECERR rather than leaving the core waiting forever.
  wire m_awvalid, m_wvalid, m_wlast, m_bready, m_arvalid, m_rready;
  wire [5:0] m_awid, m_arid;
  wire [7:0] m_arlen;
  logic ext_wr_active, ext_bvalid, ext_rvalid;
  logic [5:0] ext_bid, ext_rid;
  logic [7:0] ext_remaining;
  wire m_awready = !ext_wr_active && !ext_bvalid;
  wire m_wready = ext_wr_active && !ext_bvalid;
  wire m_arready = !ext_rvalid;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      ext_wr_active <= 0;
      ext_bvalid <= 0;
      ext_rvalid <= 0;
      ext_bid <= 0;
      ext_rid <= 0;
      ext_remaining <= 0;
    end else begin
      if (m_awvalid && m_awready) begin
        ext_wr_active <= 1;
        ext_bid <= m_awid;
      end
      if (m_wvalid && m_wready && m_wlast) begin
        ext_wr_active <= 0;
        ext_bvalid <= 1;
      end
      if (ext_bvalid && m_bready) ext_bvalid <= 0;
      if (m_arvalid && m_arready) begin
        ext_rvalid <= 1;
        ext_rid <= m_arid;
        ext_remaining <= m_arlen;
      end
      if (ext_rvalid && m_rready) begin
        if (ext_remaining == 0) ext_rvalid <= 0;
        else ext_remaining <= ext_remaining - 1'b1;
      end
    end
  end
  RvvCoreMiniAxi i_core (
      .io_aclk(clk),
      .io_aresetn(rst_n),
      .io_axi_slave_write_addr_ready(n_awready),
      .io_axi_slave_write_addr_valid(n_awvalid),
      .io_axi_slave_write_addr_bits_addr(aw_q),
      .io_axi_slave_write_addr_bits_prot(3'b0),
      .io_axi_slave_write_addr_bits_id(6'b0),
      .io_axi_slave_write_addr_bits_len(8'b0),
      .io_axi_slave_write_addr_bits_size(3'd2),
      .io_axi_slave_write_addr_bits_burst(2'b01),
      .io_axi_slave_write_addr_bits_lock(1'b0),
      .io_axi_slave_write_addr_bits_cache(4'b0),
      .io_axi_slave_write_addr_bits_qos(4'b0),
      .io_axi_slave_write_addr_bits_region(4'b0),
      .io_axi_slave_write_data_ready(n_wready),
      .io_axi_slave_write_data_valid(n_wvalid),
      .io_axi_slave_write_data_bits_data({4{wd_q}}),
      .io_axi_slave_write_data_bits_last(1'b1),
      .io_axi_slave_write_data_bits_strb(({12'b0, ws_q} << (aw_q[3:2] * 4))),
      .io_axi_slave_write_resp_ready(s_bready),
      .io_axi_slave_write_resp_valid(s_bvalid),
      .io_axi_slave_write_resp_bits_id(),
      .io_axi_slave_write_resp_bits_resp(s_bresp),
      .io_axi_slave_read_addr_ready(n_arready),
      .io_axi_slave_read_addr_valid((s_arvalid && !rd_busy)),
      .io_axi_slave_read_addr_bits_addr(s_araddr),
      .io_axi_slave_read_addr_bits_prot(3'b0),
      .io_axi_slave_read_addr_bits_id(6'b0),
      .io_axi_slave_read_addr_bits_len(8'b0),
      .io_axi_slave_read_addr_bits_size(3'd2),
      .io_axi_slave_read_addr_bits_burst(2'b01),
      .io_axi_slave_read_addr_bits_lock(1'b0),
      .io_axi_slave_read_addr_bits_cache(4'b0),
      .io_axi_slave_read_addr_bits_qos(4'b0),
      .io_axi_slave_read_addr_bits_region(4'b0),
      .io_axi_slave_read_data_ready(s_rready),
      .io_axi_slave_read_data_valid(s_rvalid),
      .io_axi_slave_read_data_bits_data(n_rdata),
      .io_axi_slave_read_data_bits_id(),
      .io_axi_slave_read_data_bits_resp(s_rresp),
      .io_axi_slave_read_data_bits_last(),
      .io_axi_master_write_addr_ready(m_awready),
      .io_axi_master_write_addr_valid(m_awvalid),
      .io_axi_master_write_addr_bits_addr(),
      .io_axi_master_write_addr_bits_prot(),
      .io_axi_master_write_addr_bits_id(m_awid),
      .io_axi_master_write_addr_bits_len(),
      .io_axi_master_write_addr_bits_size(),
      .io_axi_master_write_addr_bits_burst(),
      .io_axi_master_write_addr_bits_lock(),
      .io_axi_master_write_addr_bits_cache(),
      .io_axi_master_write_addr_bits_qos(),
      .io_axi_master_write_addr_bits_region(),
      .io_axi_master_write_data_ready(m_wready),
      .io_axi_master_write_data_valid(m_wvalid),
      .io_axi_master_write_data_bits_data(),
      .io_axi_master_write_data_bits_last(m_wlast),
      .io_axi_master_write_data_bits_strb(),
      .io_axi_master_write_resp_ready(m_bready),
      .io_axi_master_write_resp_valid(ext_bvalid),
      .io_axi_master_write_resp_bits_id(ext_bid),
      .io_axi_master_write_resp_bits_resp(2'b11),
      .io_axi_master_read_addr_ready(m_arready),
      .io_axi_master_read_addr_valid(m_arvalid),
      .io_axi_master_read_addr_bits_addr(),
      .io_axi_master_read_addr_bits_prot(),
      .io_axi_master_read_addr_bits_id(m_arid),
      .io_axi_master_read_addr_bits_len(m_arlen),
      .io_axi_master_read_addr_bits_size(),
      .io_axi_master_read_addr_bits_burst(),
      .io_axi_master_read_addr_bits_lock(),
      .io_axi_master_read_addr_bits_cache(),
      .io_axi_master_read_addr_bits_qos(),
      .io_axi_master_read_addr_bits_region(),
      .io_axi_master_read_data_ready(m_rready),
      .io_axi_master_read_data_valid(ext_rvalid),
      .io_axi_master_read_data_bits_data(128'b0),
      .io_axi_master_read_data_bits_id(ext_rid),
      .io_axi_master_read_data_bits_resp(2'b11),
      .io_axi_master_read_data_bits_last((ext_remaining == 0)),
      .io_halted(),
      .io_fault(),
      .io_wfi(),
      .io_irq(1'b0),
      .io_boot_addr(32'b0),
      .io_timer_irq(1'b0),
      .io_software_irq(1'b0),
      .io_dm_req_ready(),
      .io_dm_req_valid(1'b0),
      .io_dm_req_bits_address(32'b0),
      .io_dm_req_bits_data(32'b0),
      .io_dm_req_bits_op(2'b0),
      .io_dm_rsp_ready(1'b1),
      .io_dm_rsp_valid(),
      .io_dm_rsp_bits_data(),
      .io_dm_rsp_bits_op(),
      .io_te(1'b0)
  );
endmodule

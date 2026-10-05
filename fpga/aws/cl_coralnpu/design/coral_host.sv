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
    input wire s_rready,
    output wire [31:0] m_awaddr,
    output wire [5:0] m_awid,
    output wire [7:0] m_awlen,
    output wire [2:0] m_awsize,
    output wire [1:0] m_awburst,
    output wire m_awlock,
    output wire m_awvalid,
    input wire m_awready,
    output wire [127:0] m_wdata,
    output wire [15:0] m_wstrb,
    output wire m_wlast,
    output wire m_wvalid,
    input wire m_wready,
    input wire [5:0] m_bid,
    input wire [1:0] m_bresp,
    input wire m_bvalid,
    output wire m_bready,
    output wire [31:0] m_araddr,
    output wire [5:0] m_arid,
    output wire [7:0] m_arlen,
    output wire [2:0] m_arsize,
    output wire [1:0] m_arburst,
    output wire m_arlock,
    output wire m_arvalid,
    input wire m_arready,
    input wire [127:0] m_rdata,
    input wire [5:0] m_rid,
    input wire [1:0] m_rresp,
    input wire m_rlast,
    input wire m_rvalid,
    output wire m_rready,
    input wire ddr_ready, ddr_present, ddr_fault
);

  // Capture independent AXI-Lite address/data channels before widening WSTRB.
  logic aw_full, w_full, wr_busy, aw_sent, w_sent, rd_busy;
  logic [31:0] aw_q, wd_q;
  logic [3:0] ws_q;
  logic [1:0] read_lane;
  wire n_awready, n_wready, n_arready;
  wire [127:0] n_rdata;
  wire status_write = aw_q[31:8] == 24'h000400;
  wire core_bvalid;
  wire [1:0] core_bresp;
  wire n_awvalid = wr_busy && !aw_sent && !status_write;
  wire n_wvalid = wr_busy && !w_sent && !status_write;
  assign s_bvalid = (wr_busy && status_write) || core_bvalid;
  assign s_bresp = (wr_busy && status_write) ? 2'b11 : core_bresp;
  assign s_awready = !aw_full && !wr_busy;
  assign s_wready  = !w_full && !wr_busy;
  wire status_select = s_araddr[31:8] == 24'h000400;
  logic status_pending;
  logic [31:0] status_data;
  logic [1:0] status_resp;
  wire core_rvalid;
  wire [1:0] core_rresp;
  assign s_arready = !rd_busy && (status_select || n_arready);
  assign s_rvalid = status_pending || core_rvalid;
  assign s_rresp = status_pending ? status_resp : core_rresp;
  assign s_rdata = status_pending ? status_data : n_rdata[read_lane*32+:32];
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
      status_pending <= 0;
      status_data <= 0;
      status_resp <= 0;
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
        if (status_select) begin
          status_pending <= 1;
          status_resp <= 0;
          case (s_araddr[7:0])
            8'h00: status_data <= 32'h43444452;
            8'h04: status_data <= {29'b0,ddr_fault,ddr_present,ddr_ready};
            8'h08: status_data <= 32'h80000000;
            8'h0c: status_data <= 32'h20000000;
            8'h10: status_data <= 32'h00000001;
            default: begin status_data <= 0; status_resp <= 2'b11; end
          endcase
        end
      end
      if (s_rvalid && s_rready) begin rd_busy <= 0; status_pending <= 0; end
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
      .io_axi_slave_write_resp_ready(s_bready && !status_write),
      .io_axi_slave_write_resp_valid(core_bvalid),
      .io_axi_slave_write_resp_bits_id(),
      .io_axi_slave_write_resp_bits_resp(core_bresp),
      .io_axi_slave_read_addr_ready(n_arready),
      .io_axi_slave_read_addr_valid((s_arvalid && !rd_busy && !status_select)),
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
      .io_axi_slave_read_data_ready(s_rready && !status_pending),
      .io_axi_slave_read_data_valid(core_rvalid),
      .io_axi_slave_read_data_bits_data(n_rdata),
      .io_axi_slave_read_data_bits_id(),
      .io_axi_slave_read_data_bits_resp(core_rresp),
      .io_axi_slave_read_data_bits_last(),
      .io_axi_master_write_addr_ready(m_awready),
      .io_axi_master_write_addr_valid(m_awvalid),
      .io_axi_master_write_addr_bits_addr(m_awaddr),
      .io_axi_master_write_addr_bits_prot(),
      .io_axi_master_write_addr_bits_id(m_awid),
      .io_axi_master_write_addr_bits_len(m_awlen),
      .io_axi_master_write_addr_bits_size(m_awsize),
      .io_axi_master_write_addr_bits_burst(m_awburst),
      .io_axi_master_write_addr_bits_lock(m_awlock),
      .io_axi_master_write_addr_bits_cache(),
      .io_axi_master_write_addr_bits_qos(),
      .io_axi_master_write_addr_bits_region(),
      .io_axi_master_write_data_ready(m_wready),
      .io_axi_master_write_data_valid(m_wvalid),
      .io_axi_master_write_data_bits_data(m_wdata),
      .io_axi_master_write_data_bits_last(m_wlast),
      .io_axi_master_write_data_bits_strb(m_wstrb),
      .io_axi_master_write_resp_ready(m_bready),
      .io_axi_master_write_resp_valid(m_bvalid),
      .io_axi_master_write_resp_bits_id(m_bid),
      .io_axi_master_write_resp_bits_resp(m_bresp),
      .io_axi_master_read_addr_ready(m_arready),
      .io_axi_master_read_addr_valid(m_arvalid),
      .io_axi_master_read_addr_bits_addr(m_araddr),
      .io_axi_master_read_addr_bits_prot(),
      .io_axi_master_read_addr_bits_id(m_arid),
      .io_axi_master_read_addr_bits_len(m_arlen),
      .io_axi_master_read_addr_bits_size(m_arsize),
      .io_axi_master_read_addr_bits_burst(m_arburst),
      .io_axi_master_read_addr_bits_lock(m_arlock),
      .io_axi_master_read_addr_bits_cache(),
      .io_axi_master_read_addr_bits_qos(),
      .io_axi_master_read_addr_bits_region(),
      .io_axi_master_read_data_ready(m_rready),
      .io_axi_master_read_data_valid(m_rvalid),
      .io_axi_master_read_data_bits_data(m_rdata),
      .io_axi_master_read_data_bits_id(m_rid),
      .io_axi_master_read_data_bits_resp(m_rresp),
      .io_axi_master_read_data_bits_last(m_rlast),
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

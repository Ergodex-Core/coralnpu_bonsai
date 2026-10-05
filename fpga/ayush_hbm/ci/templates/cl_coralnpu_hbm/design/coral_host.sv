module coral_host (
    input logic clk,
    rst_n,
    input logic hbm_ready,
    axi_bus_t.slave ext,
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
  wire n_awvalid = wr_busy && !wr_local && !aw_sent;
  wire n_wvalid = wr_busy && !wr_local && !w_sent;
  assign s_awready = !aw_full && !wr_busy;
  assign s_wready  = !w_full && !wr_busy;
  assign s_arready = !rd_busy && (ar_local || n_arready);
  assign s_rdata   = rd_local ? local_rdata : n_rdata[read_lane*32+:32];
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

  // Full HBM capacity is selected in 2 GiB windows at 0x80000000.
  // Invalid low external addresses are decoded as errors by SmartConnect.
  logic [2:0] hbm_bank;
  wire [31:0] m_awaddr, m_araddr;
  assign ext.awaddr = m_awaddr[31] ? {30'b0,hbm_bank,m_awaddr[30:0]} : (64'h10_0000_0000 | {32'b0,m_awaddr});
  assign ext.araddr = m_araddr[31] ? {30'b0,hbm_bank,m_araddr[30:0]} : (64'h10_0000_0000 | {32'b0,m_araddr});
  assign ext.wid = '0;
  wire  core_halted;
  logic core_reset_held;
  logic [7:0] ext_reads, ext_writes;
  wire mem_idle = ext_reads == 0 && ext_writes == 0 && !ext.awvalid && !ext.arvalid;
  wire wr_local = aw_q[31:12] == 20'h00040;
  wire ar_local = s_araddr[31:12] == 20'h00040;
  logic rd_local, local_bvalid, local_rvalid;
  logic [ 1:0] local_bresp;
  logic [31:0] local_rdata;
  wire n_bvalid, n_rvalid;
  wire [1:0] n_bresp, n_rresp;
  assign s_bvalid = wr_busy && wr_local ? local_bvalid : n_bvalid;
  assign s_bresp  = wr_busy && wr_local ? local_bresp : n_bresp;
  assign s_rvalid = rd_local ? local_rvalid : n_rvalid;
  assign s_rresp  = rd_local ? 2'b00 : n_rresp;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      hbm_bank <= 0;
      core_reset_held <= 1;
      ext_reads <= 0;
      ext_writes <= 0;
      rd_local <= 0;
      local_bvalid <= 0;
      local_rvalid <= 0;
      local_bresp <= 0;
      local_rdata <= 0;
    end else begin
      case ({
        ext.arvalid && ext.arready, ext.rvalid && ext.rready && ext.rlast
      })
        2'b10:   ext_reads <= ext_reads + 1'b1;
        2'b01:   ext_reads <= ext_reads - 1'b1;
        default: ;
      endcase
      case ({
        ext.awvalid && ext.awready, ext.bvalid && ext.bready
      })
        2'b10:   ext_writes <= ext_writes + 1'b1;
        2'b01:   ext_writes <= ext_writes - 1'b1;
        default: ;
      endcase
      if (n_bvalid && s_bready && n_bresp == 0 && aw_q == 32'h30000 && ws_q[0])
        core_reset_held <= wd_q[0];
      if (wr_busy && wr_local && !local_bvalid) begin
        local_bvalid <= 1;
        local_bresp  <= 2'b10;
        if (aw_q == 32'h40008 && (core_reset_held || core_halted) && mem_idle) begin
          if (ws_q[0]) hbm_bank <= wd_q[2:0];
          local_bresp <= 0;
        end
      end
      if (local_bvalid && s_bready) local_bvalid <= 0;
      if (s_arvalid && s_arready) begin
        rd_local <= ar_local;
        if (ar_local) begin
          local_rvalid <= 1;
          case (s_araddr[11:0])
            12'h000: local_rdata <= 32'h48424d31;
            12'h004: local_rdata <= {29'b0, mem_idle, core_halted, hbm_ready};
            12'h008: local_rdata <= {29'b0, hbm_bank};
            12'h00c: local_rdata <= {16'b0, ext_writes, ext_reads};
            default: local_rdata <= 32'hdeadbeef;
          endcase
        end
      end
      if (local_rvalid && s_rready) local_rvalid <= 0;
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
      .io_axi_slave_write_resp_ready(s_bready && !wr_local),
      .io_axi_slave_write_resp_valid(n_bvalid),
      .io_axi_slave_write_resp_bits_id(),
      .io_axi_slave_write_resp_bits_resp(n_bresp),
      .io_axi_slave_read_addr_ready(n_arready),
      .io_axi_slave_read_addr_valid((s_arvalid && !rd_busy && !ar_local)),
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
      .io_axi_slave_read_data_ready(s_rready && !rd_local),
      .io_axi_slave_read_data_valid(n_rvalid),
      .io_axi_slave_read_data_bits_data(n_rdata),
      .io_axi_slave_read_data_bits_id(),
      .io_axi_slave_read_data_bits_resp(n_rresp),
      .io_axi_slave_read_data_bits_last(),
      .io_axi_master_write_addr_ready(ext.awready),
      .io_axi_master_write_addr_valid(ext.awvalid),
      .io_axi_master_write_addr_bits_addr(m_awaddr),
      .io_axi_master_write_addr_bits_prot(),
      .io_axi_master_write_addr_bits_id(ext.awid),
      .io_axi_master_write_addr_bits_len(ext.awlen),
      .io_axi_master_write_addr_bits_size(ext.awsize),
      .io_axi_master_write_addr_bits_burst(ext.awburst),
      .io_axi_master_write_addr_bits_lock(),
      .io_axi_master_write_addr_bits_cache(),
      .io_axi_master_write_addr_bits_qos(),
      .io_axi_master_write_addr_bits_region(),
      .io_axi_master_write_data_ready(ext.wready),
      .io_axi_master_write_data_valid(ext.wvalid),
      .io_axi_master_write_data_bits_data(ext.wdata),
      .io_axi_master_write_data_bits_last(ext.wlast),
      .io_axi_master_write_data_bits_strb(ext.wstrb),
      .io_axi_master_write_resp_ready(ext.bready),
      .io_axi_master_write_resp_valid(ext.bvalid),
      .io_axi_master_write_resp_bits_id(ext.bid),
      .io_axi_master_write_resp_bits_resp(ext.bresp),
      .io_axi_master_read_addr_ready(ext.arready),
      .io_axi_master_read_addr_valid(ext.arvalid),
      .io_axi_master_read_addr_bits_addr(m_araddr),
      .io_axi_master_read_addr_bits_prot(),
      .io_axi_master_read_addr_bits_id(ext.arid),
      .io_axi_master_read_addr_bits_len(ext.arlen),
      .io_axi_master_read_addr_bits_size(ext.arsize),
      .io_axi_master_read_addr_bits_burst(ext.arburst),
      .io_axi_master_read_addr_bits_lock(),
      .io_axi_master_read_addr_bits_cache(),
      .io_axi_master_read_addr_bits_qos(),
      .io_axi_master_read_addr_bits_region(),
      .io_axi_master_read_data_ready(ext.rready),
      .io_axi_master_read_data_valid(ext.rvalid),
      .io_axi_master_read_data_bits_data(ext.rdata),
      .io_axi_master_read_data_bits_id(ext.rid),
      .io_axi_master_read_data_bits_resp(ext.rresp),
      .io_axi_master_read_data_bits_last(ext.rlast),
      .io_halted(core_halted),
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

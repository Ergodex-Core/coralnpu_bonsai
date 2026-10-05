// Real pinned Coral RTL + production DDR path. Only SH_DDR storage and XPM
// FIFO internals are functional simulation models. No physical timing claim.
`timescale 1ns / 1ps
module tb_core_ddr;
  logic clk = 0, host_clk = 0, rst_n = 0, ddr_ready = 0;
  always #10 clk = ~clk;
  always #2 host_clk = ~host_clk;
  wire ddr_fault;
  logic [31:0] s_awaddr = 0, s_wdata = 0, s_araddr = 0;
  logic [3:0] s_wstrb = 0;
  logic s_awvalid = 0, s_wvalid = 0, s_bready = 0, s_arvalid = 0, s_rready = 0;
  wire s_awready, s_wready, s_bvalid, s_arready, s_rvalid;
  wire [1:0] s_bresp, s_rresp;
  wire [31:0] s_rdata;
  logic [31:0] c_awaddr;
  logic [5:0] c_awid;
  logic [7:0] c_awlen;
  logic [2:0] c_awsize;
  logic [1:0] c_awburst;
  logic c_awlock;
  logic c_awvalid;
  logic c_awready;
  logic [127:0] c_wdata;
  logic [15:0] c_wstrb;
  logic c_wlast;
  logic c_wvalid;
  logic c_wready;
  logic [5:0] c_bid;
  logic [1:0] c_bresp;
  logic c_bvalid;
  logic c_bready;
  logic [31:0] c_araddr;
  logic [5:0] c_arid;
  logic [7:0] c_arlen;
  logic [2:0] c_arsize;
  logic [1:0] c_arburst;
  logic c_arlock;
  logic c_arvalid;
  logic c_arready;
  logic [127:0] c_rdata;
  logic [5:0] c_rid;
  logic [1:0] c_rresp;
  logic c_rlast;
  logic c_rvalid;
  logic c_rready;
  logic [63:0] h_awaddr = 0;
  logic [15:0] h_awid = 0;
  logic [7:0] h_awlen = 0;
  logic [2:0] h_awsize = 0;
  logic [1:0] h_awburst = 0;
  logic h_awlock = 0;
  logic h_awvalid = 0;
  logic h_awready = 0;
  logic [511:0] h_wdata = 0;
  logic [63:0] h_wstrb = 0;
  logic h_wlast = 0;
  logic h_wvalid = 0;
  logic h_wready = 0;
  logic [15:0] h_bid = 0;
  logic [1:0] h_bresp = 0;
  logic h_bvalid = 0;
  logic h_bready = 0;
  logic [63:0] h_araddr = 0;
  logic [15:0] h_arid = 0;
  logic [7:0] h_arlen = 0;
  logic [2:0] h_arsize = 0;
  logic [1:0] h_arburst = 0;
  logic h_arlock = 0;
  logic h_arvalid = 0;
  logic h_arready = 0;
  logic [511:0] h_rdata = 0;
  logic [15:0] h_rid = 0;
  logic [1:0] h_rresp = 0;
  logic h_rlast = 0;
  logic h_rvalid = 0;
  logic h_rready = 0;
  logic [63:0] m_awaddr;
  logic [15:0] m_awid;
  logic [7:0] m_awlen;
  logic [2:0] m_awsize;
  logic [1:0] m_awburst;
  logic m_awvalid;
  logic m_awready;
  logic [511:0] m_wdata;
  logic [63:0] m_wstrb;
  logic m_wlast;
  logic m_wvalid;
  logic m_wready;
  logic [15:0] m_bid;
  logic [1:0] m_bresp;
  logic m_bvalid;
  logic m_bready;
  logic [63:0] m_araddr;
  logic [15:0] m_arid;
  logic [7:0] m_arlen;
  logic [2:0] m_arsize;
  logic [1:0] m_arburst;
  logic m_arvalid;
  logic m_arready;
  logic [511:0] m_rdata;
  logic [15:0] m_rid;
  logic [1:0] m_rresp;
  logic m_rlast;
  logic m_rvalid;
  logic m_rready;
  coral_host core (
      .clk(clk),
      .rst_n(rst_n),
      .ddr_ready(ddr_ready),
      .ddr_present(1'b1),
      .ddr_fault(ddr_fault),
      .s_awaddr(s_awaddr),
      .s_wdata(s_wdata),
      .s_araddr(s_araddr),
      .s_wstrb(s_wstrb),
      .s_awvalid(s_awvalid),
      .s_wvalid(s_wvalid),
      .s_bready(s_bready),
      .s_arvalid(s_arvalid),
      .s_rready(s_rready),
      .s_awready(s_awready),
      .s_wready(s_wready),
      .s_bvalid(s_bvalid),
      .s_arready(s_arready),
      .s_rvalid(s_rvalid),
      .s_bresp(s_bresp),
      .s_rresp(s_rresp),
      .s_rdata(s_rdata),
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
      .m_rready(c_rready)
  );
  coral_ddr_subsystem ddr (
      .core_clk(clk),
      .core_rst_n(rst_n),
      .host_clk(host_clk),
      .host_rst_n(rst_n),
      .ddr_ready(ddr_ready),
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
      .h_awaddr(h_awaddr),
      .h_awid(h_awid),
      .h_awlen(h_awlen),
      .h_awsize(h_awsize),
      .h_awburst(h_awburst),
      .h_awlock(h_awlock),
      .h_awvalid(h_awvalid),
      .h_awready(h_awready),
      .h_wdata(h_wdata),
      .h_wstrb(h_wstrb),
      .h_wlast(h_wlast),
      .h_wvalid(h_wvalid),
      .h_wready(h_wready),
      .h_bid(h_bid),
      .h_bresp(h_bresp),
      .h_bvalid(h_bvalid),
      .h_bready(h_bready),
      .h_araddr(h_araddr),
      .h_arid(h_arid),
      .h_arlen(h_arlen),
      .h_arsize(h_arsize),
      .h_arburst(h_arburst),
      .h_arlock(h_arlock),
      .h_arvalid(h_arvalid),
      .h_arready(h_arready),
      .h_rdata(h_rdata),
      .h_rid(h_rid),
      .h_rresp(h_rresp),
      .h_rlast(h_rlast),
      .h_rvalid(h_rvalid),
      .h_rready(h_rready),
      .m_awaddr(m_awaddr),
      .m_awid(m_awid),
      .m_awlen(m_awlen),
      .m_awsize(m_awsize),
      .m_awburst(m_awburst),
      .m_awvalid(m_awvalid),
      .m_awready(m_awready),
      .m_wdata(m_wdata),
      .m_wstrb(m_wstrb),
      .m_wlast(m_wlast),
      .m_wvalid(m_wvalid),
      .m_wready(m_wready),
      .m_bid(m_bid),
      .m_bresp(m_bresp),
      .m_bvalid(m_bvalid),
      .m_bready(m_bready),
      .m_araddr(m_araddr),
      .m_arid(m_arid),
      .m_arlen(m_arlen),
      .m_arsize(m_arsize),
      .m_arburst(m_arburst),
      .m_arvalid(m_arvalid),
      .m_arready(m_arready),
      .m_rdata(m_rdata),
      .m_rid(m_rid),
      .m_rresp(m_rresp),
      .m_rlast(m_rlast),
      .m_rvalid(m_rvalid),
      .m_rready(m_rready)
  );

  // Sparse, byte-strobed 512-bit SH_DDR model, independent AW/W capture and
  // deterministic stalls on every channel. Storage is accessible only by AXI.
  logic [511:0] memory[longint unsigned];
  logic have_aw = 0, have_w = 0;
  logic [ 63:0] aw_q;
  logic [511:0] wd_q;
  logic [ 63:0] ws_q;
  integer host_cycles = 0, ddr_reads = 0, ddr_writes = 0, core_reads = 0, core_writes = 0;
  assign m_awready = rst_n && !have_aw && !m_bvalid && host_cycles % 7 != 1;
  assign m_wready = rst_n && !have_w && !m_bvalid && host_cycles % 5 != 2;
  assign m_arready = rst_n && !m_rvalid && host_cycles % 9 != 4;
  assign m_bid = 0;
  assign m_bresp = 0;
  assign m_rid = 0;
  assign m_rresp = 0;
  assign m_rlast = 1;
  always @(posedge host_clk) begin
    host_cycles <= host_cycles + 1;
    if (!rst_n) begin
      have_aw  <= 0;
      have_w   <= 0;
      m_bvalid <= 0;
      m_rvalid <= 0;
      m_rdata  <= 0;
    end else begin
      if (m_awvalid && m_awready) begin
        if(m_awlen!=0 || m_awsize!=6 || m_awburst!=1 || m_awaddr[5:0]!=0 || m_awaddr>=64'h80000000)
          $fatal(1, "Invalid DDR AW geometry");
        aw_q <= m_awaddr;
        have_aw <= 1;
      end
      if (m_wvalid && m_wready) begin
        if (!m_wlast) $fatal(1, "Invalid DDR WLAST");
        wd_q   <= m_wdata;
        ws_q   <= m_wstrb;
        have_w <= 1;
      end
      if (have_aw && have_w && !m_bvalid && host_cycles % 11 != 3) begin
        if (!memory.exists(aw_q)) memory[aw_q] = '0;
        for (int i = 0; i < 64; i++) if (ws_q[i]) memory[aw_q][i*8+:8] = wd_q[i*8+:8];
        have_aw <= 0;
        have_w <= 0;
        m_bvalid <= 1;
        ddr_writes <= ddr_writes + 1;
      end
      if (m_bvalid && m_bready) m_bvalid <= 0;
      if (m_arvalid && m_arready) begin
        if(m_arlen!=0 || m_arsize!=6 || m_arburst!=1 || m_araddr[5:0]!=0 || m_araddr>=64'h80000000)
          $fatal(1, "Invalid DDR AR geometry");
        m_rdata   <= memory.exists(m_araddr) ? memory[m_araddr] : '0;
        m_rvalid  <= 1;
        ddr_reads <= ddr_reads + 1;
      end
      if (m_rvalid && m_rready) m_rvalid <= 0;
    end
  end
  always @(posedge clk) begin
    if (c_arvalid && c_arready) core_reads <= core_reads + 1;
    if (c_awvalid && c_awready) core_writes <= core_writes + 1;
    if (rst_n && ddr_fault) $fatal(1, "DDR backend fault");
  end

  task automatic wr(input logic [31:0] a, input logic [31:0] d);
    @(negedge clk);
    s_awaddr  = a;
    s_awvalid = 1;
    do @(posedge clk); while (!s_awready);
    @(negedge clk);
    s_awvalid = 0;
    s_wdata   = d;
    s_wstrb   = 15;
    s_wvalid  = 1;
    do @(posedge clk); while (!s_wready);
    @(negedge clk);
    s_wvalid = 0;
    wait (s_bvalid);
    if (s_bresp != 0) $fatal(1, "BAR0 write failed %x", a);
    @(negedge clk);
    s_bready = 1;
    @(posedge clk);
    @(negedge clk);
    s_bready = 0;
  endtask
  task automatic rd(input logic [31:0] a, output logic [31:0] d);
    @(negedge clk);
    s_araddr  = a;
    s_arvalid = 1;
    do @(posedge clk); while (!s_arready);
    @(negedge clk);
    s_arvalid = 0;
    wait (s_rvalid);
    d = s_rdata;
    if (s_rresp != 0) $fatal(1, "BAR0 read failed %x", a);
    @(negedge clk);
    s_rready = 1;
    @(posedge clk);
    @(negedge clk);
    s_rready = 0;
  endtask
  task automatic host_write(input logic [63:0] a, input logic [511:0] d);
    @(negedge host_clk);
    h_awaddr  = a;
    h_awsize  = 6;
    h_awburst = 1;
    h_awvalid = 1;
    do @(posedge host_clk); while (!h_awready);
    @(negedge host_clk);
    h_awvalid = 0;
    h_wdata   = d;
    h_wstrb   = '1;
    h_wlast   = 1;
    h_wvalid  = 1;
    do @(posedge host_clk); while (!h_wready);
    @(negedge host_clk);
    h_wvalid = 0;
    wait (h_bvalid);
    if (h_bresp != 0) $fatal(1, "PCIS write failed %x response%x", a, h_bresp);
    repeat (3) @(negedge host_clk);
    h_bready = 1;
    @(posedge host_clk);
    @(negedge host_clk);
    h_bready = 0;
  endtask
  task automatic host_read(input logic [63:0] a, output logic [511:0] d);
    @(negedge host_clk);
    h_araddr  = a;
    h_arsize  = 6;
    h_arburst = 1;
    h_arvalid = 1;
    do @(posedge host_clk); while (!h_arready);
    @(negedge host_clk);
    h_arvalid = 0;
    wait (h_rvalid);
    d = h_rdata;
    repeat (3) begin
      @(negedge host_clk);
      if (!h_rvalid || h_rdata !== d) $fatal(1, "PCIS read unstable");
    end
    if (h_rresp != 0 || !h_rlast) $fatal(1, "PCIS read failed %x", a);
    h_rready = 1;
    @(posedge host_clk);
    @(negedge host_clk);
    h_rready = 0;
  endtask
  string tcm_file, ddr_file, reads_file;
  integer fd, n, limit = 2000000;
  logic [31:0] address, word, status;
  logic [511:0] line_data;
  initial begin
    if (!$value$plusargs(
            "tcm=%s", tcm_file
        ) || !$value$plusargs(
            "ddr=%s", ddr_file
        ) || !$value$plusargs(
            "reads=%s", reads_file
        ))
      $fatal(1, "Missing fixture paths");
    n = $value$plusargs("limit=%d", limit);
    repeat (10) @(negedge clk);
    rst_n = 1;
    repeat (30) @(negedge clk);
    rd('h40004, status);
    if (status != 2) $fatal(1, "Unexpected pre-calibration status%x", status);
    wr('h30000, 1);
    fd = $fopen(tcm_file, "r");
    if (!fd) $fatal(1, "TCM fixture unavailable");
    while (!$feof(
        fd
    )) begin
      n = $fscanf(fd, "%h %h\n", address, word);
      if (n == 2) wr(address, word);
      else if (n != -1) $fatal(1, "Malformed TCM fixture");
    end
    $fclose(fd);
    // Calibration completion precedes the first host transfer.
    ddr_ready = 1;
    fd = $fopen(ddr_file, "r");
    if (!fd) $fatal(1, "DDR fixture unavailable");
    while (!$feof(
        fd
    )) begin
      n = $fscanf(fd, "%h %h\n", address, line_data);
      if (n == 2) host_write({32'b0, address}, line_data);
      else if (n != -1) $fatal(1, "Malformed DDR fixture");
    end
    $fclose(fd);
    rd('h40004, status);
    if (status != 3) $fatal(1, "DDR not ready%x", status);
    wr('h30004, 0);
    wr('h30000, 0);
    status = 0;
    for (int poll = 0; poll < limit; poll++) begin
      rd('h30008, status);
      if (status[1]) $fatal(1, "Core fault status%x", status);
      if (status[0]) break;
    end
    if (!status[0]) $fatal(1, "Core did not halt within poll limit");
    fd = $fopen(reads_file, "r");
    if (!fd) $fatal(1, "Readback fixture unavailable");
    while (!$feof(
        fd
    )) begin
      n = $fscanf(fd, "%h\n", address);
      if (n == 1) begin
        if (address >= 32'h20000000 && address < 32'ha0000000) begin
          host_read({32'b0, ((address - 32'h20000000) & 32'hffffffc0)}, line_data);
          word = line_data[address[5:2]*32+:32];
        end else rd(address, word);
        $display("READBACK %08x %08x", address, word);
      end else if (n != -1) $fatal(1, "Malformed readback fixture");
    end
    $fclose(fd);
    if (core_reads == 0 || core_writes == 0) $fatal(1, "Core did not use DDR for reads and writes");
    $display("COUNTS core_reads=%0d core_writes=%0d ddr_reads=%0d ddr_writes=%0d", core_reads,
             core_writes, ddr_reads, ddr_writes);
    $display(
        "PASS: real Coral core boot, DDR instruction/data access, host load/readback and halt");
    $finish;
  end
endmodule

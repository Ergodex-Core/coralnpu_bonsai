`timescale 1ns / 1ps
module tb_ddr_backend;
  logic clk = 0, rst_n = 0, ddr_ready = 0;
  always #5 clk = ~clk;
  wire fault;
  logic [1:0] req_valid = 0, req_write = 0, rsp_ready = 0;
  wire [1:0] req_ready, rsp_valid, rsp_resp;
  logic [1:0][31:0] req_addr = 0;
  logic [1:0][511:0] req_wdata = 0;
  logic [1:0][63:0] req_wstrb = 0;
  wire [511:0] rsp_rdata;
  wire [15:0] m_awid, m_arid;
  wire [63:0] m_awaddr, m_araddr;
  wire [7:0] m_awlen, m_arlen;
  wire [2:0] m_awsize, m_arsize;
  wire [1:0] m_awburst, m_arburst;
  wire m_awvalid, m_wvalid, m_wlast, m_arvalid, m_bready, m_rready;
  wire [511:0] m_wdata;
  wire [ 63:0] m_wstrb;
  logic m_awready = 0, m_wready = 0, m_arready = 0, m_bvalid = 0, m_rvalid = 0, m_rlast = 1;
  logic [15:0] m_bid = 0, m_rid = 0;
  logic [1:0] m_bresp = 0, m_rresp = 0;
  logic [511:0] m_rdata = 0;
  coral_ddr_backend #(.TIMEOUT_CYCLES(32)) dut (.*);
  localparam [511:0] PAYLOAD = {16{32'hc09a1357}};
  localparam [63:0] STROBES = 64'hffaa5aa55aa5aaff;
  integer responses = 0;
  logic held_aw = 0, held_w = 0, held_ar = 0, held_rsp = 0;
  logic [92:0] saved_aw, saved_ar;
  logic [576:0] saved_w;
  logic [515:0] saved_rsp;
  always @(posedge clk) begin
    if (rst_n) begin
      if (held_aw && (!m_awvalid || {m_awaddr, m_awid, m_awlen, m_awsize, m_awburst} !== saved_aw))
        $fatal(1, "AW changed while stalled");
      if (held_w && (!m_wvalid || {m_wdata, m_wstrb, m_wlast} !== saved_w))
        $fatal(1, "W changed while stalled");
      if (held_ar && (!m_arvalid || {m_araddr, m_arid, m_arlen, m_arsize, m_arburst} !== saved_ar))
        $fatal(1, "AR changed while stalled");
      if (held_rsp && ({rsp_valid, rsp_rdata, rsp_resp} !== saved_rsp))
        $fatal(1, "Line response changed while stalled");
      if ((req_valid & req_ready) == 3 || rsp_valid == 3) $fatal(1, "Arbiter selected both owners");
      if (|(rsp_valid & rsp_ready)) responses = responses + 1;
    end
    held_aw <= rst_n && m_awvalid && !m_awready;
    held_w <= rst_n && m_wvalid && !m_wready;
    held_ar <= rst_n && m_arvalid && !m_arready;
    held_rsp <= rst_n && |(rsp_valid & ~rsp_ready);
    saved_aw <= {m_awaddr, m_awid, m_awlen, m_awsize, m_awburst};
    saved_ar <= {m_araddr, m_arid, m_arlen, m_arsize, m_arburst};
    saved_w <= {m_wdata, m_wstrb, m_wlast};
    saved_rsp <= {rsp_valid, rsp_rdata, rsp_resp};
  end
  task automatic reset_dut;
    @(negedge clk);
    rst_n = 0;
    ddr_ready = 0;
    req_valid = 0;
    rsp_ready = 0;
    m_awready = 0;
    m_wready = 0;
    m_arready = 0;
    m_bvalid = 0;
    m_rvalid = 0;
    m_bid = 0;
    m_rid = 0;
    m_bresp = 0;
    m_rresp = 0;
    m_rlast = 1;
    repeat (4) @(negedge clk);
    if (fault || req_ready || rsp_valid || m_awvalid || m_wvalid || m_arvalid)
      $fatal(1, "Reset failed");
    rst_n = 1;
    repeat (3) @(negedge clk);
  endtask
  task automatic request(input bit owner, input bit wr, input logic [31:0] addr);
    @(negedge clk);
    req_addr[owner]  = addr;
    req_write[owner] = wr;
    req_wdata[owner] = PAYLOAD;
    req_wstrb[owner] = STROBES;
    req_valid[owner] = 1;
    do @(posedge clk); while (!req_ready[owner]);
    @(negedge clk);
    req_valid[owner] = 0;
  endtask
  task automatic check_stop;
    @(negedge clk);
    req_valid   = 3;
    req_addr[0] = 128;
    req_addr[1] = 192;
    repeat (40) begin
      @(negedge clk);
      if (req_ready || rsp_valid) $fatal(1, "Fail-stop accepted work or repeated response");
    end
    req_valid = 0;
    if (!fault) $fatal(1, "Fault was not sticky");
  endtask
  task automatic response(input bit owner, input logic [1:0] resp, input bit data_valid,
                          input bit expected_fault);
    wait (rsp_valid[owner]);
    repeat (6) @(negedge clk);
    if (rsp_valid !== (2'b01 << owner) || rsp_resp !== resp || fault !== expected_fault)
      $fatal(
          1,
          "Response mismatch owner=%0d rsp=%h resp=%h/%h fault=%b/%b",
          owner,
          rsp_valid,
          rsp_resp,
          resp,
          fault,
          expected_fault
      );
    if (data_valid && rsp_rdata !== PAYLOAD) $fatal(1, "Read response data mismatch");
    if (!data_valid && rsp_rdata !== 0) $fatal(1, "Expected cleared response data");
    rsp_ready[owner] = 1;
    @(negedge clk);
    rsp_ready = 0;
  endtask
  task automatic accept_read(input logic [31:0] addr, input integer delay_cycles);
    wait (m_arvalid);
    repeat (delay_cycles) @(negedge clk);
    if(m_araddr!=={32'b0,addr} || m_arlen!==0 || m_arsize!==6 || m_arburst!==1 || m_arid!==0)
      $fatal(1, "Wrong SH_DDR read geometry");
    m_arready = 1;
    @(negedge clk);
    m_arready = 0;
  endtask
  task automatic deliver_read(input logic [1:0] resp, input logic [15:0] id, input bit last);
    @(negedge clk);
    m_rdata = PAYLOAD;
    m_rresp = resp;
    m_rid = id;
    m_rlast = last;
    m_rvalid = 1;
    do @(posedge clk); while (!m_rready);
    @(negedge clk);
    m_rvalid = 0;
  endtask
  task automatic accept_write(input logic [31:0] addr, input bit data_first);
    wait (m_awvalid && m_wvalid);
    repeat (4) @(negedge clk);
    if(m_awaddr!=={32'b0,addr} || m_awlen!==0 || m_awsize!==6 || m_awburst!==1 || m_awid!==0)
      $fatal(1, "Wrong SH_DDR write geometry");
    if (m_wdata !== PAYLOAD || m_wstrb !== STROBES || !m_wlast)
      $fatal(1, "Wrong write data/strobes");
    if (data_first) m_wready = 1;
    else m_awready = 1;
    @(negedge clk);
    m_wready  = 0;
    m_awready = 0;
    repeat (3) @(negedge clk);
    if (m_bready) $fatal(1, "B accepted before both AW and W");
    if (data_first) m_awready = 1;
    else m_wready = 1;
    @(negedge clk);
    m_awready = 0;
    m_wready  = 0;
  endtask
  task automatic deliver_write(input logic [1:0] resp, input logic [15:0] id);
    @(negedge clk);
    m_bresp = resp;
    m_bid = id;
    m_bvalid = 1;
    do @(posedge clk); while (!m_bready);
    @(negedge clk);
    m_bvalid = 0;
  endtask
  initial begin
    #1000000;
    $fatal(1, "Backend simulation timeout");
  end
  initial begin
    reset_dut();
    // Requests may wait for calibration; no AXI activity before ready.
    fork
      request(0, 0, 32'h40);
      begin
        repeat (40) begin
          @(negedge clk);
          if (req_ready || m_arvalid || fault) $fatal(1, "Access before calibration");
        end
        ddr_ready = 1;
      end
    join
    accept_read(32'h40, 4);
    deliver_read(0, 0, 1);
    response(0, 0, 1, 0);
    // Both AXI write channel orders and all native response values.
    for (integer error = 0; error < 4; error = error + 1) begin
      request(error % 2, 1, 32'h80);
      accept_write(32'h80, error % 2);
      deliver_write(error, 0);
      response(error % 2, error, 0, 0);
      request(error % 2, 0, 32'hc0);
      accept_read(32'hc0, 3);
      deliver_read(error, 0, 1);
      response(error % 2, error, 1, 0);
    end
    // Fair arbitration is round-robin when both sources request repeatedly.
    @(negedge clk);
    req_valid   = 3;
    req_write   = 0;
    req_addr[0] = 32'h100;
    req_addr[1] = 32'h140;
    for (integer turn = 0; turn < 8; turn = turn + 1) begin
      do @(posedge clk); while (!(req_ready & req_valid));
      if (req_ready !== (2'b01 << (turn % 2)))
        $fatal(1, "Round robin starvation turn=%0d ready=%b", turn, req_ready);
      @(negedge clk);
      accept_read(turn % 2 ? 32'h140 : 32'h100, 2);
      deliver_read(0, 0, 1);
      response(turn % 2, 0, 1, 0);
    end
    req_valid = 0;
    // Defensive line aperture guards: reject no-wrap and unaligned addresses.
    request(0, 0, 32'h80000000);
    response(0, 3, 0, 0);
    request(1, 1, 32'h41);
    response(1, 3, 0, 0);
    request(0, 0, 32'h7fffffc0);
    accept_read(32'h7fffffc0, 1);
    deliver_read(0, 0, 1);
    response(0, 0, 1, 0);
    // Corrupt read ID poisons the port; reset is the only recovery.
    request(1, 0, 32'h40);
    accept_read(32'h40, 1);
    deliver_read(0, 1, 1);
    response(1, 2, 1, 1);
    check_stop();
    reset_dut();
    ddr_ready = 1;
    request(0, 1, 32'h40);
    accept_write(32'h40, 1);
    deliver_write(0, 7);
    response(0, 2, 0, 1);
    check_stop();
    reset_dut();
    ddr_ready = 1;
    // Missing RLAST generates one failure, then safely drains late beats.
    request(0, 0, 32'h40);
    accept_read(32'h40, 1);
    deliver_read(0, 0, 0);
    response(0, 2, 1, 1);
    deliver_read(0, 0, 0);
    deliver_read(0, 0, 1);
    check_stop();
    reset_dut();
    ddr_ready = 1;
    // Timeout with AR stalled: keep AR stable, later drain, never reuse ID.
    request(1, 0, 32'h40);
    response(1, 2, 0, 1);
    if (!m_arvalid) $fatal(1, "Timeout withdrew ARVALID");
    accept_read(32'h40, 2);
    deliver_read(0, 0, 1);
    check_stop();
    reset_dut();
    ddr_ready = 1;
    // Timeout after AR accepted but before R arrives.
    request(0, 0, 32'h80);
    accept_read(32'h80, 2);
    response(0, 2, 0, 1);
    deliver_read(0, 0, 1);
    check_stop();
    reset_dut();
    ddr_ready = 1;
    // Timeout with both independent write channels held.
    request(0, 1, 32'h100);
    response(0, 2, 0, 1);
    if (!m_awvalid || !m_wvalid) $fatal(1, "Timeout withdrew AW/WVALID");
    accept_write(32'h100, 1);
    deliver_write(0, 0);
    check_stop();
    reset_dut();
    ddr_ready = 1;
    // Calibration loss during an accepted transaction also fails closed.
    request(1, 0, 32'h80);
    accept_read(32'h80, 1);
    ddr_ready = 0;
    response(1, 2, 0, 1);
    ddr_ready = 1;
    deliver_read(0, 0, 1);
    check_stop();
    reset_dut();
    ddr_ready = 1;
    request(0, 1, 32'h40);
    wait (m_awvalid);
    reset_dut();
    ddr_ready = 1;
    request(1, 0, 32'h40);
    accept_read(32'h40, 1);
    deliver_read(0, 0, 1);
    response(1, 0, 1, 0);
    $display(
        "PASS: backend responses=%0d arbitration calibration channels backpressure errors watchdog late-drain reset",
        responses);
    $finish;
  end
endmodule

`timescale 1ns / 1ps
module tb_host;
  logic clk = 0, rst_n = 0;
  always #10 clk = ~clk;
  logic [31:0] s_awaddr = 0, s_wdata = 0, s_araddr = 0;
  logic [3:0] s_wstrb = 0;
  logic s_awvalid = 0, s_wvalid = 0, s_bready = 0, s_arvalid = 0, s_rready = 0;
  wire s_awready, s_wready, s_bvalid, s_arready, s_rvalid;
  wire [1:0] s_bresp, s_rresp;
  wire [31:0] s_rdata;
  logic ddr_ready=0, ddr_present=0, ddr_fault=0;
  // This regression exercises TCM/CSRs only. External AXI is covered by the
  // separate DDR subsystem and real-core DDR simulations.
  coral_host dut (
    .m_awaddr(),.m_awid(),.m_awlen(),.m_awsize(),.m_awburst(),.m_awlock(),.m_awvalid(),
    .m_awready(1'b0),.m_wdata(),.m_wstrb(),.m_wlast(),.m_wvalid(),.m_wready(1'b0),
    .m_bid(6'b0),.m_bresp(2'b0),.m_bvalid(1'b0),.m_bready(),
    .m_araddr(),.m_arid(),.m_arlen(),.m_arsize(),.m_arburst(),.m_arlock(),.m_arvalid(),
    .m_arready(1'b0),.m_rdata(128'b0),.m_rid(6'b0),.m_rresp(2'b0),
    .m_rlast(1'b0),.m_rvalid(1'b0),.m_rready(),.*
  );
  task automatic aw(input logic [31:0] a);
    @(negedge clk);
    s_awaddr  = a;
    s_awvalid = 1;
    do @(posedge clk); while (!s_awready);
    @(negedge clk);
    s_awvalid = 0;
  endtask
  task automatic wd(input logic [31:0] d, input logic [3:0] st);
    @(negedge clk);
    s_wdata  = d;
    s_wstrb  = st;
    s_wvalid = 1;
    do @(posedge clk); while (!s_wready);
    @(negedge clk);
    s_wvalid = 0;
  endtask
  task automatic wr(input logic [31:0] a, input logic [31:0] d, input logic [3:0] st = 15,
                    input bit data_first = 0, input logic [1:0] expected_resp = 0);
    if (data_first) begin
      wd(d, st);
      repeat (3) @(posedge clk);
      aw(a);
    end else begin
      aw(a);
      repeat (3) @(posedge clk);
      wd(d, st);
    end
    wait (s_bvalid);
    repeat (4) begin
      @(posedge clk);
      if (!s_bvalid) $fatal(1, "BVALID dropped under backpressure");
    end
    if (s_bresp != expected_resp) $fatal(1, "Write error addr=%x resp=%x expected=%x", a, s_bresp, expected_resp);
    @(negedge clk);
    s_bready = 1;
    @(posedge clk);
    @(negedge clk);
    s_bready = 0;
  endtask
  task automatic rd(input logic [31:0] a, output logic [31:0] d,
                    input logic [1:0] expected_resp = 0);
    @(negedge clk);
    s_araddr  = a;
    s_arvalid = 1;
    do @(posedge clk); while (!s_arready);
    @(negedge clk);
    s_arvalid = 0;
    wait (s_rvalid);
    d = s_rdata;
    repeat (4) begin
      @(posedge clk);
      if (!s_rvalid || s_rdata !== d) $fatal(1, "Read unstable under backpressure");
    end
    if (s_rresp != expected_resp) $fatal(1, "Read error addr=%x resp=%x expected=%x", a, s_rresp, expected_resp);
    @(negedge clk);
    s_rready = 1;
    @(posedge clk);
    @(negedge clk);
    s_rready = 0;
  endtask
  logic [31:0] value;
  initial begin
    #10000000;
    $fatal(1, "Simulation timeout");
  end
  initial begin
    repeat (10) @(negedge clk);
    rst_n = 1;
    repeat (20) @(negedge clk);
    rd('h30000, value);
    if (value != 3) $fatal(1, "Reset CSR %x", value);
    rd('h40000, value);
    if (value != 'h43444452) $fatal(1, "DDR identity %x", value);
    rd('h40008, value);
    if (value != 'h80000000) $fatal(1, "DDR aperture %x", value);
    rd('h4000c, value);
    if (value != 'h20000000) $fatal(1, "DDR CPU base %x", value);
    rd('h40010, value);
    if (value != 1) $fatal(1, "DDR ABI version %x", value);
    for (int status=0; status<8; status++) begin
      {ddr_fault,ddr_present,ddr_ready}=3'(status);
      rd('h40004, value);
      if (value != status) $fatal(1, "DDR status actual=%x expected=%x", value, status);
    end
    {ddr_fault,ddr_present,ddr_ready}=0;
    rd('h40014, value, 3);
    wr('h40000, 'hffffffff, 15, 0, 3);
    wr('h40004, 'hffffffff, 15, 1, 3);
    rd('h40000, value);
    if (value != 'h43444452) $fatal(1, "DDR identity changed after rejected write");
    wr('h30000, 1);
    for (int i = 0; i < 4; i++) begin
      wr('h10000 + 4 * i, 32'h10203040 + i, 15, i % 2);
      rd('h10000 + 4 * i, value);
      if (value !== 32'h10203040 + i) $fatal(1, "Lane %0d failed %x", i, value);
    end
    wr('h10004, 'haabbccdd, 4'b0101, 1);
    rd('h10004, value);
    if (value !== 32'h10bb30dd) $fatal(1, "Byte strobes %x", value);
    for (int run = 0; run < 2; run++) begin
      wr('h30000, 1);
      wr(0, 32'h02a00093 + (run << 20));  // addi x1,x0,42/43
      wr(4, 32'h00010137);  // lui x2,0x10
      wr(8, 32'h00112023);  // sw x1,0(x2)
      wr(12, 32'h0ff0000f);  // fence
      wr(16, 32'h08000073);  // mpause
      wr('h30004, 0);
      wr('h30000, 0);
      value = 0;
      for (int tries = 0; tries < 1000; tries++) begin
        rd('h30008, value);
        if (value[1]) $fatal(1, "Core fault run %d status %x", run, value);
        if (value[0]) break;
      end
      if (!value[0]) $fatal(1, "Core did not halt");
      rd('h10000, value);
      if (value !== 42 + run) $fatal(1, "Wrong execution result %x", value);
      $display("RUN %0d PASS result=%0d", run, value);
    end
    wr('h30000, 1);
    for (int i = 0; i < 4; i++) begin
      wr('h10000 + 4 * i, 10 * (i + 1));
      wr('h10010 + 4 * i, i + 1);
      wr('h10020 + 4 * i, 0);
    end
    wr(0, 32'h60000293);
    wr(4, 32'h3002a073);
    wr(8, 32'h00010537);
    wr(12, 32'h01050593);
    wr(16, 32'h02050613);
    wr(20, 32'hcd027057);
    wr(24, 32'h02056087);
    wr(28, 32'h0205e107);
    wr(32, 32'h021101d7);
    wr(36, 32'h020661a7);
    wr(40, 32'h0ff0000f);
    wr(44, 32'h08000073);
    wr('h30004, 0);
    wr('h30000, 0);
    value = 0;
    for (int tries = 0; tries < 1000; tries++) begin
      rd('h30008, value);
      if (value[1]) $fatal(1, "Vector program fault %x", value);
      if (value[0]) break;
    end
    if (!value[0]) $fatal(1, "Vector program did not halt");
    for (int i = 0; i < 4; i++) begin
      rd('h10020 + 4 * i, value);
      if (value !== 11 * (i + 1)) $fatal(1, "Vector lane %0d wrong result %x", i, value);
    end
    $display("PASS: RVV vector addition [10,20,30,40]+[1,2,3,4]=[11,22,33,44]");
    $display(
        "PASS: TCM lanes, byte strobes, independent AW/W, response backpressure, execution and restart");
    $finish;
  end
endmodule

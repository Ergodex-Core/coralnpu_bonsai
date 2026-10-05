`timescale 1ns / 1ps
module tb_hbm;
  logic clk = 0, rst_n = 0;
  always #10 clk = ~clk;
  logic [31:0] s_awaddr = 0, s_wdata = 0, s_araddr = 0;
  logic [3:0] s_wstrb = 0;
  logic s_awvalid = 0, s_wvalid = 0, s_bready = 0, s_arvalid = 0, s_rready = 0;
  wire s_awready, s_wready, s_bvalid, s_arready, s_rvalid;
  wire [1:0] s_bresp, s_rresp;
  wire [31:0] s_rdata;
  axi_bus_t #(
      .DATA_WIDTH(128),
      .ADDR_WIDTH(64),
      .ID_WIDTH  (6)
  ) ext ();
  wire hbm_ready = 1;
  coral_host dut (.*);
  hbm_memory_model ram (
      .clk  (clk),
      .rst_n(rst_n),
      .bus  (ext)
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
    if (s_bresp != expected_resp) $fatal(1, "Write error addr=%x resp=%x", a, s_bresp);
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
    repeat (4) begin
      @(posedge clk);
      if (!s_rvalid || s_rdata !== d) $fatal(1, "Read unstable under backpressure");
    end
    if (s_rresp != 0) $fatal(1, "Read error addr=%x resp=%x", a, s_rresp);
    @(negedge clk);
    s_rready = 1;
    @(posedge clk);
    @(negedge clk);
    s_rready = 0;
  endtask
  logic [31:0] value;
  longint unsigned base;
  integer bank;
  initial begin
    #500000000;
    $fatal(1, "Simulation timeout");
  end
  initial begin
    repeat (10) @(negedge clk);
    rst_n = 1;
    repeat (20) @(negedge clk);
    rd('h30000, value);
    if (value != 3) $fatal(1, "Reset CSR %x", value);
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

    rd('h40000, value);
    if (value != 32'h48424d31) $fatal(1, "HBM signature wrong");
    for (int run = 0; run < 2; run++) begin
      bank = run == 0 ? 0 : 7;
      base = 64'(bank) << 31;
      wr('h30000, 1);
      wr('h40008, bank, 15, run % 2);
      rd('h40008, value);
      if (value != bank) $fatal(1, "Bank readback mismatch");
      ram.put_word(base + 0, 32'h60000293);
      ram.put_word(base + 4, 32'h3002a073);
      ram.put_word(base + 8, 32'h80010537);
      ram.put_word(base + 12, 32'h800305b7);
      ram.put_word(base + 16, 32'h80050637);
      ram.put_word(base + 20, 32'h00004337);
      ram.put_word(base + 24, 32'hcd027057);
      ram.put_word(base + 28, 32'h02056087);
      ram.put_word(base + 32, 32'h0205e107);
      ram.put_word(base + 36, 32'h021101d7);
      ram.put_word(base + 40, 32'h020661a7);
      ram.put_word(base + 44, 32'h01050513);
      ram.put_word(base + 48, 32'h01058593);
      ram.put_word(base + 52, 32'h01060613);
      ram.put_word(base + 56, 32'hffc30313);
      ram.put_word(base + 60, 32'hfe0310e3);
      ram.put_word(base + 64, 32'h0ff0000f);
      ram.put_word(base + 68, 32'h00010537);
      ram.put_word(base + 72, 32'h05100293);
      ram.put_word(base + 76, 32'h00552023);
      ram.put_word(base + 80, 32'h0ff0000f);
      ram.put_word(base + 84, 32'h08000073);

      for (int i = 0; i < 16384; i++) begin
        ram.put_word(base + 'h10000 + 4 * i, i);
        ram.put_word(base + 'h30000 + 4 * i, 3 * i + 7 + bank * 65536);
        ram.put_word(base + 'h50000 + 4 * i, 0);
      end
      wr('h10000, 0);
      wr('h30004, 32'h80000000);
      wr('h30000, 0);
      // Changing the mapping under a running program must fail.
      wr('h40008, bank ^ 1, 15, 0, 2'b10);
      rd('h40008, value);
      if (value != bank) $fatal(1, "Active bank changed during execution");
      value = 0;
      for (int tries = 0; tries < 200000; tries++) begin
        rd('h30008, value);
        if (value[1]) $fatal(1, "External-memory program fault: %x", value);
        if (value[0]) break;
      end
      if (!value[0]) $fatal(1, "External-memory program did not halt");
      for (int i = 0; i < 16384; i++) begin
        if (ram.get_word(base + 'h50000 + 4 * i) !== 4 * i + 7 + bank * 65536)
          $fatal(
              1,
              "HBM bank %0d vector element %0d mismatch %x",
              bank,
              i,
              ram.get_word(
                  base + 'h50000 + 4 * i
              )
          );
      end
      rd('h10000, value);
      if (value != 'h51) $fatal(1, "HBM completion marker missing");
      rd('h4000c, value);
      if (value != 0) $fatal(1, "Outstanding memory requests remain");
      $display(
          "PASS: HBM bank %0d, external instruction fetch, 16384-element vector addition, 192 KiB of external data",
          bank);
    end
    for (int i = 0; i < 16384; i++)
    if (ram.get_word('h50000 + 4 * i) !== 4 * i + 7)
      $fatal(1, "High-bank access aliased bank zero");
    $display(
        "PASS: HBM bank isolation, active bank-change rejection, stalled AXI memory responses");
    $finish;
  end
endmodule

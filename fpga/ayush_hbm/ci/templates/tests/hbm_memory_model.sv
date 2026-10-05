// AXI behavioral memory for core/bridge verification, not a model of HBM timing.
module hbm_memory_model (
    input logic clk,
    rst_n,
    axi_bus_t.master bus
);
  byte unsigned mem[longint unsigned];
  longint unsigned wa, ra;
  logic wactive, ractive;
  logic [7:0] wleft, rleft;
  logic [2:0] wsize, rsize;
  logic [5:0] wid, rid;
  integer cycle = 0;
  assign bus.awready = rst_n && !wactive && !bus.bvalid && cycle % 3 != 0;
  assign bus.wready  = rst_n && wactive && cycle % 5 != 0;
  assign bus.arready = rst_n && !ractive && !bus.rvalid && cycle % 4 != 0;
  function automatic logic [31:0] get_word(input longint unsigned a);
    logic [31:0] v;
    for (int j = 0; j < 4; j++) v[8*j+:8] = mem.exists(a + j) ? mem[a+j] : 0;
    return v;
  endfunction
  task automatic put_word(input longint unsigned a, input logic [31:0] v);
    for (int j = 0; j < 4; j++) mem[a+j] = v[8*j+:8];
  endtask
  always @(posedge clk) begin
    cycle <= cycle + 1;
    if (!rst_n) begin
      wactive <= 0;
      ractive <= 0;
      bus.bvalid <= 0;
      bus.rvalid <= 0;
      bus.bid <= 0;
      bus.bresp <= 0;
      bus.rid <= 0;
      bus.rdata <= 0;
      bus.rresp <= 0;
      bus.rlast <= 0;
      wa <= 0;
      ra <= 0;
      wleft <= 0;
      rleft <= 0;
      wsize <= 0;
      rsize <= 0;
      wid <= 0;
      rid <= 0;
    end else begin
      if (bus.awvalid && bus.awready) begin
        if (bus.awlen != 0 && bus.awburst != 1) $fatal(1, "Unsupported memory-model burst");
        wa <= bus.awaddr;
        wsize <= bus.awsize;
        wleft <= bus.awlen;
        wid <= bus.awid;
        wactive <= 1;
      end
      if (bus.wvalid && bus.wready) begin
        if (bus.wlast != (wleft == 0)) $fatal(1, "WLAST mismatch");
        if (wa < 64'h4_0000_0000)
          for (int j = 0; j < 16; j++) if (bus.wstrb[j]) mem[(wa&~64'hf)+j] = bus.wdata[8*j+:8];
        if (bus.wlast) begin
          wactive <= 0;
          bus.bvalid <= 1;
          bus.bid <= wid;
          bus.bresp <= wa < 64'h4_0000_0000 ? 0 : 3;
        end else begin
          wa <= (wa & ~((64'd1 << wsize) - 1)) + (64'd1 << wsize);
          wleft <= wleft - 1'b1;
        end
      end
      if (bus.bvalid && bus.bready) bus.bvalid <= 0;
      if (bus.arvalid && bus.arready) begin
        if (bus.arlen != 0 && bus.arburst != 1) $fatal(1, "Unsupported memory-model burst");
        ra <= bus.araddr;
        rsize <= bus.arsize;
        rleft <= bus.arlen;
        rid <= bus.arid;
        ractive <= 1;
      end
      if (ractive && !bus.rvalid && cycle % 3 != 0) begin
        bus.rvalid <= 1;
        bus.rid <= rid;
        bus.rlast <= rleft == 0;
        bus.rresp <= ra < 64'h4_0000_0000 ? 0 : 3;
        for (int j = 0; j < 16; j++)
        bus.rdata[8*j+:8] <= mem.exists((ra & ~64'hf) + j) ? mem[(ra&~64'hf)+j] : 0;
      end
      if (bus.rvalid && bus.rready) begin
        bus.rvalid <= 0;
        if (rleft == 0) ractive <= 0;
        else begin
          ra <= (ra & ~((64'd1 << rsize) - 1)) + (64'd1 << rsize);
          rleft <= rleft - 1'b1;
        end
      end
    end
  end
endmodule

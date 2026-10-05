`timescale 1ns/1ps
module tb_fabric;
  logic npu_clk=0,host_clk=0,resetn=0;
  always #10 npu_clk=~npu_clk;
  always #2 host_clk=~host_clk;
  axi_bus_t #(.DATA_WIDTH(128),.ADDR_WIDTH(64),.ID_WIDTH(6)) npu();
  axi_bus_t #(.DATA_WIDTH(512),.ADDR_WIDTH(64),.ID_WIDTH(6)) host();
  axi_bus_t hbm();
  axi_bus_t #(.DATA_WIDTH(256),.ADDR_WIDTH(64),.ID_WIDTH(6),.LEN_WIDTH(4)) mem();
  logic hbm_clk=0; always #(5.0/3.0) hbm_clk=~hbm_clk;
  assign mem.awid=0;assign mem.arid=0;assign mem.wid=0;
  assign hbm.awid=0; assign hbm.arid=0; assign hbm.wid=0;
  fabric_memory_model ram(.clk(hbm_clk),.rst_n(resetn),.bus(mem));
  coral_hbm_fabric_wrapper dut(
    .npu_clk(npu_clk),.host_clk(host_clk),.resetn(resetn),
.NPU_awid(npu.awid),
.NPU_awaddr(npu.awaddr),
.NPU_awlen(npu.awlen),
.NPU_awsize(npu.awsize),
.NPU_awburst(npu.awburst),
.NPU_awvalid(npu.awvalid),
.NPU_awready(npu.awready),
.NPU_wdata(npu.wdata),
.NPU_wstrb(npu.wstrb),
.NPU_wlast(npu.wlast),
.NPU_wvalid(npu.wvalid),
.NPU_wready(npu.wready),
.NPU_bid(npu.bid),
.NPU_bresp(npu.bresp),
.NPU_bvalid(npu.bvalid),
.NPU_bready(npu.bready),
.NPU_arid(npu.arid),
.NPU_araddr(npu.araddr),
.NPU_arlen(npu.arlen),
.NPU_arsize(npu.arsize),
.NPU_arburst(npu.arburst),
.NPU_arvalid(npu.arvalid),
.NPU_arready(npu.arready),
.NPU_rid(npu.rid),
.NPU_rdata(npu.rdata),
.NPU_rresp(npu.rresp),
.NPU_rlast(npu.rlast),
.NPU_rvalid(npu.rvalid),
.NPU_rready(npu.rready),
.NPU_awlock(1'b0),
.NPU_awcache(4'b0011),
.NPU_awprot(3'b0),
.NPU_awqos(4'b0),
.NPU_arlock(1'b0),
.NPU_arcache(4'b0011),
.NPU_arprot(3'b0),
.NPU_arqos(4'b0),
.HOST_awid(host.awid),
.HOST_awaddr(host.awaddr),
.HOST_awlen(host.awlen),
.HOST_awsize(host.awsize),
.HOST_awburst(host.awburst),
.HOST_awvalid(host.awvalid),
.HOST_awready(host.awready),
.HOST_wdata(host.wdata),
.HOST_wstrb(host.wstrb),
.HOST_wlast(host.wlast),
.HOST_wvalid(host.wvalid),
.HOST_wready(host.wready),
.HOST_bid(host.bid),
.HOST_bresp(host.bresp),
.HOST_bvalid(host.bvalid),
.HOST_bready(host.bready),
.HOST_arid(host.arid),
.HOST_araddr(host.araddr),
.HOST_arlen(host.arlen),
.HOST_arsize(host.arsize),
.HOST_arburst(host.arburst),
.HOST_arvalid(host.arvalid),
.HOST_arready(host.arready),
.HOST_rid(host.rid),
.HOST_rdata(host.rdata),
.HOST_rresp(host.rresp),
.HOST_rlast(host.rlast),
.HOST_rvalid(host.rvalid),
.HOST_rready(host.rready),
.HOST_awlock(1'b0),
.HOST_awcache(4'b0011),
.HOST_awprot(3'b0),
.HOST_awqos(4'b0),
.HOST_arlock(1'b0),
.HOST_arcache(4'b0011),
.HOST_arprot(3'b0),
.HOST_arqos(4'b0),
.HBM_awaddr(hbm.awaddr),
.HBM_awlen(hbm.awlen),
.HBM_awsize(hbm.awsize),
.HBM_awburst(hbm.awburst),
.HBM_awvalid(hbm.awvalid),
.HBM_awready(hbm.awready),
.HBM_wdata(hbm.wdata),
.HBM_wstrb(hbm.wstrb),
.HBM_wlast(hbm.wlast),
.HBM_wvalid(hbm.wvalid),
.HBM_wready(hbm.wready),
.HBM_bresp(hbm.bresp),
.HBM_bvalid(hbm.bvalid),
.HBM_bready(hbm.bready),
.HBM_araddr(hbm.araddr),
.HBM_arlen(hbm.arlen),
.HBM_arsize(hbm.arsize),
.HBM_arburst(hbm.arburst),
.HBM_arvalid(hbm.arvalid),
.HBM_arready(hbm.arready),
.HBM_rdata(hbm.rdata),
.HBM_rresp(hbm.rresp),
.HBM_rlast(hbm.rlast),
.HBM_rvalid(hbm.rvalid),
.HBM_rready(hbm.rready),
.HBM_awlock(),
.HBM_awcache(),
.HBM_awprot(),
.HBM_awqos(),
.HBM_arlock(),
.HBM_arcache(),
.HBM_arprot(),
.HBM_arqos()
);

  task automatic npu_write(input logic[63:0] address,input int beats,input logic[31:0] seed,input logic[1:0] expected=0);
    @(negedge npu_clk);
    npu.awid=6'h15; npu.awaddr=address; npu.awlen=beats-1; npu.awsize=4; npu.awburst=1; npu.awvalid=1;
    do @(posedge npu_clk); while(!npu.awready);
    @(negedge npu_clk); npu.awvalid=0;
    for(int beat=0;beat<beats;beat++) begin
      repeat(beat%3) @(negedge npu_clk);
      for(int lane=0;lane<4;lane++) npu.wdata[32*lane+:32]=seed+beat*4+lane;
      npu.wstrb='1; npu.wlast=beat==beats-1; npu.wvalid=1;
      do @(posedge npu_clk); while(!npu.wready);
      @(negedge npu_clk); npu.wvalid=0;
    end
    wait(npu.bvalid);
    repeat(4) begin @(posedge npu_clk); if(!npu.bvalid) $fatal(1,"Write response dropped"); end
    if(npu.bresp!=expected || npu.bid!=6'h15) $fatal(1,"Write response/ID error");
    @(negedge npu_clk); npu.bready=1;
    @(posedge npu_clk); @(negedge npu_clk); npu.bready=0;
  endtask
  task automatic npu_read(input logic[63:0] address,input int beats,input logic[31:0] seed,input logic[1:0] expected=0);
    logic[127:0] held;
    @(negedge npu_clk);
    npu.arid=6'h2a; npu.araddr=address; npu.arlen=beats-1; npu.arsize=4; npu.arburst=1; npu.arvalid=1;
    do @(posedge npu_clk); while(!npu.arready);
    @(negedge npu_clk); npu.arvalid=0;
    for(int beat=0;beat<beats;beat++) begin
      wait(npu.rvalid); held=npu.rdata;
      repeat(3) begin @(posedge npu_clk); if(!npu.rvalid || npu.rdata!==held) $fatal(1,"Read response unstable"); end
      if(npu.rresp!=expected || npu.rid!=6'h2a || npu.rlast!=(beat==beats-1)) $fatal(1,"Read response/ID/last error");
      if(expected==0) for(int lane=0;lane<4;lane++)
        if(held[32*lane+:32]!==seed+beat*4+lane) $fatal(1,"npu read mismatch address=%x beat=%0d lane=%0d got=%x",address,beat,lane,held[32*lane+:32]);
      @(negedge npu_clk); npu.rready=1;
      @(posedge npu_clk); @(negedge npu_clk); npu.rready=0;
    end
  endtask

  task automatic host_write(input logic[63:0] address,input int beats,input logic[31:0] seed,input logic[1:0] expected=0);
    @(negedge host_clk);
    host.awid=6'h15; host.awaddr=address; host.awlen=beats-1; host.awsize=6; host.awburst=1; host.awvalid=1;
    do @(posedge host_clk); while(!host.awready);
    @(negedge host_clk); host.awvalid=0;
    for(int beat=0;beat<beats;beat++) begin
      repeat(beat%3) @(negedge host_clk);
      for(int lane=0;lane<16;lane++) host.wdata[32*lane+:32]=seed+beat*16+lane;
      host.wstrb='1; host.wlast=beat==beats-1; host.wvalid=1;
      do @(posedge host_clk); while(!host.wready);
      @(negedge host_clk); host.wvalid=0;
    end
    wait(host.bvalid);
    repeat(4) begin @(posedge host_clk); if(!host.bvalid) $fatal(1,"Write response dropped"); end
    if(host.bresp!=expected || host.bid!=6'h15) $fatal(1,"Write response/ID error");
    @(negedge host_clk); host.bready=1;
    @(posedge host_clk); @(negedge host_clk); host.bready=0;
  endtask
  task automatic host_read(input logic[63:0] address,input int beats,input logic[31:0] seed,input logic[1:0] expected=0);
    logic[511:0] held;
    @(negedge host_clk);
    host.arid=6'h2a; host.araddr=address; host.arlen=beats-1; host.arsize=6; host.arburst=1; host.arvalid=1;
    do @(posedge host_clk); while(!host.arready);
    @(negedge host_clk); host.arvalid=0;
    for(int beat=0;beat<beats;beat++) begin
      wait(host.rvalid); held=host.rdata;
      repeat(3) begin @(posedge host_clk); if(!host.rvalid || host.rdata!==held) $fatal(1,"Read response unstable"); end
      if(host.rresp!=expected || host.rid!=6'h2a || host.rlast!=(beat==beats-1)) $fatal(1,"Read response/ID/last error");
      if(expected==0) for(int lane=0;lane<16;lane++)
        if(held[32*lane+:32]!==seed+beat*16+lane) $fatal(1,"host read mismatch address=%x beat=%0d lane=%0d got=%x",address,beat,lane,held[32*lane+:32]);
      @(negedge host_clk); host.rready=1;
      @(posedge host_clk); @(negedge host_clk); host.rready=0;
    end
  endtask


  cl_axi_sc_1x1_wrapper hbm_converter(
.AXI3_araddr(mem.araddr),
.AXI3_arburst(mem.arburst),
.AXI3_arcache(),
.AXI3_arlen(mem.arlen),
.AXI3_arlock(),
.AXI3_arprot(),
.AXI3_arqos(),
.AXI3_arready(mem.arready),
.AXI3_arsize(mem.arsize),
.AXI3_arvalid(mem.arvalid),
.AXI3_awaddr(mem.awaddr),
.AXI3_awburst(mem.awburst),
.AXI3_awcache(),
.AXI3_awlen(mem.awlen),
.AXI3_awlock(),
.AXI3_awprot(),
.AXI3_awqos(),
.AXI3_awready(mem.awready),
.AXI3_awsize(mem.awsize),
.AXI3_awvalid(mem.awvalid),
.AXI3_bready(mem.bready),
.AXI3_bresp(mem.bresp),
.AXI3_bvalid(mem.bvalid),
.AXI3_rdata(mem.rdata),
.AXI3_rlast(mem.rlast),
.AXI3_rready(mem.rready),
.AXI3_rresp(mem.rresp),
.AXI3_rvalid(mem.rvalid),
.AXI3_wdata(mem.wdata),
.AXI3_wlast(mem.wlast),
.AXI3_wready(mem.wready),
.AXI3_wstrb(mem.wstrb),
.AXI3_wvalid(mem.wvalid),
.AXI4_araddr(hbm.araddr),
.AXI4_arburst(hbm.arburst),
.AXI4_arcache(4'b0011),
.AXI4_arid(hbm.arid),
.AXI4_arlen(hbm.arlen),
.AXI4_arlock(1'b0),
.AXI4_arprot(3'b0),
.AXI4_arqos(4'b0),
.AXI4_arready(hbm.arready),
.AXI4_arsize(hbm.arsize),
.AXI4_arvalid(hbm.arvalid),
.AXI4_awaddr(hbm.awaddr),
.AXI4_awburst(hbm.awburst),
.AXI4_awcache(4'b0011),
.AXI4_awid(hbm.awid),
.AXI4_awlen(hbm.awlen),
.AXI4_awlock(1'b0),
.AXI4_awprot(3'b0),
.AXI4_awqos(4'b0),
.AXI4_awready(hbm.awready),
.AXI4_awsize(hbm.awsize),
.AXI4_awvalid(hbm.awvalid),
.AXI4_bid(hbm.bid),
.AXI4_bready(hbm.bready),
.AXI4_bresp(hbm.bresp),
.AXI4_bvalid(hbm.bvalid),
.AXI4_rdata(hbm.rdata),
.AXI4_rid(hbm.rid),
.AXI4_rlast(hbm.rlast),
.AXI4_rready(hbm.rready),
.AXI4_rresp(hbm.rresp),
.AXI4_rvalid(hbm.rvalid),
.AXI4_wdata(hbm.wdata),
.AXI4_wlast(hbm.wlast),
.AXI4_wready(hbm.wready),
.AXI4_wstrb(hbm.wstrb),
.AXI4_wvalid(hbm.wvalid),
.aclk_250(host_clk),
.aclk_450(hbm_clk),
.aresetn_250(resetn)
);

  task automatic host_word_write(input logic[63:0] a,input logic[31:0] v,input logic[3:0] st=15);
    @(negedge host_clk); host.awaddr=a;host.awid=6'h13;host.awlen=0;host.awsize=2;host.awburst=1;host.awvalid=1;
    do @(posedge host_clk); while(!host.awready);
    @(negedge host_clk); host.awvalid=0;host.wdata={16{v}};host.wstrb=64'(st)<<a[5:0];host.wlast=1;host.wvalid=1;
    do @(posedge host_clk); while(!host.wready);
    @(negedge host_clk); host.wvalid=0;host.bready=1;
    do @(posedge host_clk); while(!host.bvalid);
    if(host.bresp!=0 || host.bid!=6'h13) $fatal(1,"Host narrow write failed");
    @(negedge host_clk);host.bready=0;
  endtask
  task automatic host_word_read(input logic[63:0] a,input logic[31:0] expected);
    @(negedge host_clk);host.araddr=a;host.arid=6'h23;host.arlen=0;host.arsize=2;host.arburst=1;host.arvalid=1;
    do @(posedge host_clk);while(!host.arready);
    @(negedge host_clk);host.arvalid=0;host.rready=1;
    do @(posedge host_clk);while(!host.rvalid);
    if(host.rresp!=0 || host.rid!=6'h23 || host.rdata[8*a[5:0]+:32]!==expected) $fatal(1,"Host narrow read failed");
    @(negedge host_clk);host.rready=0;
  endtask

  initial begin #1000000; $fatal(1,"Fabric simulation timeout"); end
  initial begin
    npu.awid=0;
    npu.awaddr=0;
    npu.awlen=0;
    npu.awsize=0;
    npu.awburst=0;
    npu.awvalid=0;
    npu.wid=0;
    npu.wdata=0;
    npu.wstrb=0;
    npu.wlast=0;
    npu.wvalid=0;
    npu.bready=0;
    npu.arid=0;
    npu.araddr=0;
    npu.arlen=0;
    npu.arsize=0;
    npu.arburst=0;
    npu.arvalid=0;
    npu.rready=0;
    host.awid=0;
    host.awaddr=0;
    host.awlen=0;
    host.awsize=0;
    host.awburst=0;
    host.awvalid=0;
    host.wid=0;
    host.wdata=0;
    host.wstrb=0;
    host.wlast=0;
    host.wvalid=0;
    host.bready=0;
    host.arid=0;
    host.araddr=0;
    host.arlen=0;
    host.arsize=0;
    host.arburst=0;
    host.arvalid=0;
    host.rready=0;

    repeat(100) @(negedge host_clk); resetn=1;
    repeat(100) @(negedge npu_clk);
    for(int lane=0;lane<16;lane++) begin
      host_word_write(64'h3_8000_8000+4*lane,32'h12340000+lane);
      host_word_read(64'h3_8000_8000+4*lane,32'h12340000+lane);
    end
    host_word_write(64'h3_8000_8004,32'haabbccdd,4'b0101);
    host_word_read(64'h3_8000_8004,32'h12bb00dd);
    host_write(64'h3_8001_0000,4,32'h1000);
    npu_read(64'h3_8001_0000,16,32'h1000);
    npu_write(64'h3_8002_0010,7,32'h2000);
    npu_read(64'h3_8002_0010,7,32'h2000);
    // Full-width host reads cover NPU-written data after width conversion.
    npu_write(64'h3_8003_0000,16,32'h3000);
    host_read(64'h3_8003_0000,4,32'h3000);
    fork
      begin npu_write(64'h40000,16,32'h4000); npu_read(64'h40000,16,32'h4000); end
      begin host_write(64'h3_8005_0000,4,32'h5000); host_read(64'h3_8005_0000,4,32'h5000); end
    join
    npu_read(64'h10_0000_0000,1,0,3);
    host_read(64'h4_0000_0000,1,0,3);
    host_write(64'h4_0000_0000,1,0,3);
    $display("PASS: both production SmartConnects, 50/250/300MHz CDC, 128/512/256-bit conversion, AXI4-to-AXI3 bursts, host narrow accesses/byte strobes, concurrent host/NPU requests, high HBM addresses, IDs and DECERR");
    $finish;
  end
endmodule

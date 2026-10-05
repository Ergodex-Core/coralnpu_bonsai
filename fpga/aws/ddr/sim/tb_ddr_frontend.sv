`timescale 1ns/1ps
// Independent byte-addressed scoreboard; exercise the same protocol at the
// core's 128-bit bus and the shell's 512-bit PCIS bus. Deliberate stalls are
// deterministic so a failure is reproducible without a waveform or seed.
module tb_ddr_frontend #(
  parameter DATA_WIDTH=128, ADDR_WIDTH=32, ID_WIDTH=6,
  parameter [63:0] BASE_ADDR=64'h20000000
);
  localparam BUS_BYTES=DATA_WIDTH/8, BUS_SIZE=$clog2(BUS_BYTES), MEM_BYTES=8192;
  logic clk=0, rst_n=0;
  always #5 clk=~clk;
  logic [ADDR_WIDTH-1:0] s_awaddr=0, s_araddr=0;
  logic [ID_WIDTH-1:0] s_awid=0, s_arid=0;
  logic [7:0] s_awlen=0, s_arlen=0;
  logic [2:0] s_awsize=0, s_arsize=0;
  logic [1:0] s_awburst=1, s_arburst=1;
  logic s_awlock=0, s_arlock=0, s_awvalid=0, s_arvalid=0;
  wire s_awready,s_arready;
  logic [DATA_WIDTH-1:0] s_wdata=0;
  logic [BUS_BYTES-1:0] s_wstrb=0;
  logic s_wlast=0,s_wvalid=0,s_bready=0,s_rready=0;
  wire s_wready,s_bvalid,s_rvalid,s_rlast;
  wire [ID_WIDTH-1:0] s_bid,s_rid;
  wire [1:0] s_bresp,s_rresp;
  wire [DATA_WIDTH-1:0] s_rdata;
  wire req_valid,req_write,rsp_ready;
  logic req_ready=0,rsp_valid=0;
  wire [31:0] req_addr;
  wire [511:0] req_wdata;
  wire [63:0] req_wstrb;
  logic [511:0] rsp_rdata=0;
  logic [1:0] rsp_resp=0;
  coral_ddr_frontend #(.DATA_WIDTH(DATA_WIDTH),.ADDR_WIDTH(ADDR_WIDTH),
    .ID_WIDTH(ID_WIDTH),.BASE_ADDR(BASE_ADDR)) dut(.*);

  byte unsigned memory[0:MEM_BYTES-1], gold[0:MEM_BYTES-1];
  integer cycle=0, request_count=0, countdown=0, expected_requests=0;
  logic pending=0, pause_requests=0;
  logic [1:0] inject_resp=0;
  logic [63:0] expected_addr=0;
  integer expected_step=0;
  logic expected_write=0;
  always @(posedge clk) begin
    if (!rst_n) begin
      cycle <= 0; req_ready <= 0; rsp_valid <= 0;
      pending <= 0; countdown <= 0;
    end else begin
      cycle <= cycle+1;
      req_ready <= !pause_requests && !pending && !rsp_valid && cycle%5!=0 && cycle%7!=0;
      if (req_valid && req_ready) begin
        if (!expected_requests) $fatal(1,"Unexpected DDR request %h",req_addr);
        if (req_addr !== ((expected_addr-BASE_ADDR)&64'hffffffc0))
          $fatal(1,"Wrong translated address actual=%h expected=%h",req_addr,expected_addr-BASE_ADDR);
        if (req_write !== expected_write) $fatal(1,"Wrong request direction");
        expected_requests = expected_requests-1;
        expected_addr = expected_addr+expected_step;
        request_count = request_count+1;
        for(integer b=0;b<64;b=b+1) begin
          rsp_rdata[b*8+:8] <= memory[(req_addr+b)%MEM_BYTES];
          if(req_write && req_wstrb[b] && inject_resp==0)
            memory[(req_addr+b)%MEM_BYTES] <= req_wdata[b*8+:8];
        end
        rsp_resp <= inject_resp;
        countdown <= 2+(cycle%4); pending <= 1; req_ready <= 0;
      end
      if(pending) begin
        if(countdown==0) begin pending<=0; rsp_valid<=1; end
        else countdown<=countdown-1;
      end
      if(rsp_valid && rsp_ready) rsp_valid<=0;
    end
  end
  // AXI payloads must remain stable for every stalled valid cycle.
  logic held_req=0,held_r=0,held_b=0;
  logic [608:0] saved_req;
  logic [DATA_WIDTH+ID_WIDTH+2:0] saved_r;
  logic [ID_WIDTH+1:0] saved_b;
  always @(posedge clk) begin
    if(rst_n) begin
      if(held_req && (!req_valid || {req_write,req_addr,req_wdata,req_wstrb}!==saved_req))
        $fatal(1,"Request changed under backpressure");
      if(held_r && (!s_rvalid || {s_rdata,s_rid,s_rresp,s_rlast}!==saved_r))
        $fatal(1,"R changed under backpressure");
      if(held_b && (!s_bvalid || {s_bid,s_bresp}!==saved_b))
        $fatal(1,"B changed under backpressure");
    end
    held_req <= rst_n && req_valid && !req_ready;
    held_r <= rst_n && s_rvalid && !s_rready;
    held_b <= rst_n && s_bvalid && !s_bready;
    saved_req <= {req_write,req_addr,req_wdata,req_wstrb};
    saved_r <= {s_rdata,s_rid,s_rresp,s_rlast};
    saved_b <= {s_bid,s_bresp};
  end

  task automatic reset_dut;
    @(negedge clk); rst_n=0;
    s_awvalid=0;s_wvalid=0;s_arvalid=0;s_bready=0;s_rready=0;
    expected_requests=0;pause_requests=0;inject_resp=0;
    repeat(4) @(negedge clk);
    if(s_bvalid || s_rvalid || req_valid) $fatal(1,"Reset did not flush outputs");
    rst_n=1;repeat(3) @(negedge clk);
  endtask
  task automatic expect_lines(input logic[63:0] addr,input integer count,input integer step,input bit wr);
    if(expected_requests!=0) $fatal(1,"Previous requests not completed");
    expected_addr=addr;expected_requests=count;expected_step=step;expected_write=wr;
  endtask
  task automatic aw(input logic[63:0] addr,input integer len,input integer size,
                    input integer burst,input bit locked);
    @(negedge clk); s_awaddr=addr;s_awlen=len;s_awsize=size;s_awburst=burst;
    s_awlock=locked;s_awid=ID_WIDTH'('h35a5);s_awvalid=1;
    do @(posedge clk); while(!s_awready);
    @(negedge clk);s_awvalid=0;
  endtask
  task automatic wd(input logic[DATA_WIDTH-1:0] data,input logic[BUS_BYTES-1:0] strb,input bit last);
    @(negedge clk);s_wdata=data;s_wstrb=strb;s_wlast=last;s_wvalid=1;
    do @(posedge clk);while(!s_wready);
    @(negedge clk);s_wvalid=0;
  endtask
  task automatic bresp(input logic[1:0] resp);
    wait(s_bvalid); repeat(6) @(negedge clk);
    if(s_bresp!==resp || s_bid!==ID_WIDTH'('h35a5))
      $fatal(1,"B mismatch resp=%h expected=%h id=%h",s_bresp,resp,s_bid);
    s_bready=1;@(negedge clk);s_bready=0;
    if(expected_requests!=0) $fatal(1,"Missing DDR write requests %0d",expected_requests);
  endtask
  task automatic write_burst(input logic[63:0] addr,input integer len,input integer size,
                           input integer burst,input bit data_first,input bit partial);
    logic[DATA_WIDTH-1:0] data;
    logic[BUS_BYTES-1:0] strb;
    logic[63:0] a;
    expect_lines(addr,len+1,burst==1 ? 1<<size : 0,1);
    if(data_first) begin
      fork
        begin repeat(7) @(negedge clk);aw(addr,len,size,burst,0);end
        begin
          for(integer beat=0;beat<=len;beat=beat+1) begin
            a=addr+(burst==1 ? beat*(1<<size):0);
            data=0;strb=0;
            for(integer b=0;b<BUS_BYTES;b=b+1) begin
              data[b*8+:8]=(a+b+beat*13)^8'ha7;
              strb[b]=(b>=(a%BUS_BYTES) && b<(a%BUS_BYTES)+(1<<size)) && (!partial || b%3!=1);
              if(strb[b]) gold[((a-BASE_ADDR)&~(BUS_BYTES-1))+b]=data[b*8+:8];
            end
            wd(data,strb,beat==len);
          end
        end
      join
    end else begin
      aw(addr,len,size,burst,0); repeat(3) @(negedge clk);
      for(integer beat=0;beat<=len;beat=beat+1) begin
        a=addr+(burst==1 ? beat*(1<<size):0);data=0;strb=0;
        for(integer b=0;b<BUS_BYTES;b=b+1) begin
          data[b*8+:8]=(a+b+beat*13)^8'ha7;
          strb[b]=(b>=(a%BUS_BYTES) && b<(a%BUS_BYTES)+(1<<size)) && (!partial || b%3!=1);
          if(strb[b]) gold[((a-BASE_ADDR)&~(BUS_BYTES-1))+b]=data[b*8+:8];
        end
        wd(data,strb,beat==len);
      end
    end
    bresp(0);
  endtask
  task automatic read_burst(input logic[63:0] addr,input integer len,input integer size,
                          input integer burst,input bit locked,input logic[1:0] resp,
                          input integer lines);
    logic[DATA_WIDTH-1:0] expected;
    logic[63:0] a;
    expect_lines(addr,lines,burst==1 ? 1<<size:0,0);
    @(negedge clk);s_araddr=addr;s_arlen=len;s_arsize=size;s_arburst=burst;
    s_arlock=locked;s_arid=ID_WIDTH'('h3abc);s_arvalid=1;
    do @(posedge clk);while(!s_arready);
    @(negedge clk);s_arvalid=0;
    for(integer beat=0;beat<=len;beat=beat+1) begin
      wait(s_rvalid);repeat(5) @(negedge clk);
      a=addr+(burst==1 ? beat*(1<<size):0);expected=0;
      if(resp==0 || (lines==1 && beat==0))
        for(integer b=0;b<BUS_BYTES;b=b+1)
          expected[b*8+:8]=gold[(((a-BASE_ADDR)&~(BUS_BYTES-1))+b)%MEM_BYTES];
      if(s_rresp!==resp || s_rid!==ID_WIDTH'('h3abc) || s_rlast!==(beat==len))
        $fatal(1,"R metadata addr=%h beat=%0d resp=%h/%h id=%h last=%b",addr,beat,s_rresp,resp,s_rid,s_rlast);
      if(s_rdata!==expected) $fatal(1,"R data addr=%h beat=%0d actual=%h expected=%h",addr,beat,s_rdata,expected);
      s_rready=1;@(negedge clk);s_rready=0;
    end
    if(expected_requests!=0) $fatal(1,"Missing DDR read requests");
  endtask

  initial begin #3000000;$fatal(1,"Simulation timeout");end
  initial begin
    for(integer b=0;b<MEM_BYTES;b=b+1)begin memory[b]=(b*7)^8'h5c;gold[b]=(b*7)^8'h5c;end
    reset_dut();
    // Every natural narrow lane, partial bytes, and WVALID before AWVALID.
    for(integer size=0;size<=BUS_SIZE;size=size+1)
      for(integer lane=0;lane<BUS_BYTES;lane=lane+(1<<size)) begin
        write_burst(BASE_ADDR+256+lane,0,size,1,lane%2,size>0);
        read_burst(BASE_ADDR+256+lane,0,size,1,0,0,1);
      end
    write_burst(BASE_ADDR+48,9,4,1,1,0);
    read_burst(BASE_ADDR+48,9,4,1,0,0,10);
    write_burst(BASE_ADDR+512,15,BUS_SIZE,1,0,1);
    read_burst(BASE_ADDR+512,15,BUS_SIZE,1,0,0,16);
    write_burst(BASE_ADDR+2048,15,2,0,1,0);
    read_burst(BASE_ADDR+2048,15,2,0,0,0,16);
    // Maximum INCR len, ending exactly at a legal 4-KiB boundary.
    write_burst(BASE_ADDR+3072,255,2,1,0,1);
    read_burst(BASE_ADDR+3072,255,2,1,0,0,256);
    // Bounds and unsupported geometry must never issue a downstream request.
    read_burst(BASE_ADDR+64'h80000000,2,2,1,0,3,0);
    read_burst(BASE_ADDR+64'h7ffffff0,1,4,1,0,3,0);
    read_burst(BASE_ADDR+4092,1,2,1,0,3,0);
    read_burst(BASE_ADDR+3,0,2,1,0,3,0);
    read_burst(BASE_ADDR+64,0,BUS_SIZE+1,1,0,3,0);
    read_burst(BASE_ADDR+64,0,2,2,0,3,0);
    read_burst(BASE_ADDR+64,0,2,3,0,3,0);
    read_burst(BASE_ADDR+64,16,2,0,0,3,0);
    read_burst(BASE_ADDR+64,0,2,1,1,3,0);
    if(BASE_ADDR!=0) read_burst(BASE_ADDR-4,0,2,1,0,3,0);
    if(ADDR_WIDTH==64) read_burst(64'h100000000,0,2,1,0,3,0);
    // Highest valid address verifies subtraction without allocating 2 GiB.
    read_burst(BASE_ADDR+64'h7ffffff0,0,4,1,0,0,1);
    expect_lines(BASE_ADDR+4092,0,0,1);aw(BASE_ADDR+4092,1,2,1,0);
    wd('1,'0,0);wd('1,'0,1);bresp(3);
    // Invalid strobe and malformed WLAST never commit the offending beat.
    expect_lines(BASE_ADDR+68,0,0,1);aw(BASE_ADDR+68,0,2,1,0);wd('1,'1,1);bresp(2);
    expect_lines(BASE_ADDR+64,0,0,1);aw(BASE_ADDR+64,3,2,1,0);wd('1,'0,1);bresp(2);
    expect_lines(BASE_ADDR+64,0,0,1);aw(BASE_ADDR+64,0,2,1,0);wd('1,'0,0);
    repeat(5)@(negedge clk);if(s_bvalid)$fatal(1,"Missing LAST not drained");
    wd('1,'0,1);bresp(2);
    // Controller errors propagate, preserve ID, and stop later beat accesses.
    inject_resp=2;read_burst(BASE_ADDR+64,3,2,1,0,2,1);
    inject_resp=3;read_burst(BASE_ADDR+64,3,2,1,0,3,1);
    inject_resp=2;expect_lines(BASE_ADDR+64,1,4,1);aw(BASE_ADDR+64,2,2,1,0);
    wd('1,'0,0);wd('1,'0,0);wd('1,'0,1);bresp(2);inject_resp=0;
    // Reset cancels a stalled request and a stalled source response.
    pause_requests=1;expect_lines(BASE_ADDR+64,1,0,1);aw(BASE_ADDR+64,0,2,1,0);
    wd('1,'0,1);wait(req_valid);reset_dut();
    read_burst(BASE_ADDR+64,0,2,1,0,0,1);
    expect_lines(BASE_ADDR+64,1,0,1);aw(BASE_ADDR+64,0,2,1,0);wd('1,'0,1);
    wait(s_bvalid);reset_dut();read_burst(BASE_ADDR+64,0,2,1,0,0,1);
    for(integer b=0;b<MEM_BYTES;b=b+1)
      if(memory[b]!==gold[b])$fatal(1,"Memory corruption byte=%0d actual=%h expected=%h",b,memory[b],gold[b]);
    $display("PASS: frontend width=%0d requests=%0d lanes bursts AW/W stalls IDs RESP bounds malformed reset",DATA_WIDTH,request_count);
    $finish;
  end
endmodule

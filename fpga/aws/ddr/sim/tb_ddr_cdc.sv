`timescale 1ns/1ps
// Functional CDC wiring/ordering test only. The portable FIFO model cannot
// qualify AMD XPM implementation timing or metastability behavior.
module tb_ddr_cdc;
  logic s_clk=0,m_clk=0,s_rst_n=0,m_rst_n=0;
  always #11 s_clk=~s_clk;
  always #3 m_clk=~m_clk;
  logic s_req_valid=0,s_req_write=0,s_rsp_ready=0;
  wire s_req_ready,s_rsp_valid;
  logic [31:0] s_req_addr=0;
  logic [511:0] s_req_wdata=0;
  logic [63:0] s_req_wstrb=0;
  wire [511:0] s_rsp_rdata;
  wire [1:0] s_rsp_resp;
  wire m_req_valid,m_req_write,m_rsp_ready;
  logic m_req_ready=0,m_rsp_valid=0;
  wire [31:0] m_req_addr;
  wire [511:0] m_req_wdata;
  wire [63:0] m_req_wstrb;
  logic [511:0] m_rsp_rdata=0;
  logic [1:0] m_rsp_resp=0;
  coral_ddr_cdc dut(.*);
  integer sent_requests=0,got_requests=0,sent_responses=0,got_responses=0;
  integer epoch=1;
  bit drain_requests=0,drain_responses=0;
  integer mc=0,sc=0;
  function automatic [511:0] pattern(input integer i);
    for(integer word=0;word<16;word=word+1)
      pattern[word*32+:32]=32'h13570000^(i*113)^(word*7813)^(epoch<<24);
  endfunction
  always @(negedge m_clk)begin
    mc=mc+1;m_req_ready=drain_requests && mc%5!=0;
  end
  always @(negedge s_clk)begin
    sc=sc+1;s_rsp_ready=drain_responses && sc%7!=0;
  end
  always @(posedge m_clk)if(m_rst_n && m_req_valid && m_req_ready)begin
    if(m_req_addr!==got_requests*64 || m_req_write!==(got_requests%2!=0) ||
       m_req_wdata!==pattern(got_requests) || m_req_wstrb!==(64'h9a5512ff1234a55a^got_requests))
      $fatal(1,"CDC request mismatch/loss/order at %0d epoch=%0d",got_requests,epoch);
    got_requests=got_requests+1;
  end
  always @(posedge s_clk)if(s_rst_n && s_rsp_valid && s_rsp_ready)begin
    if(s_rsp_rdata!==pattern(got_responses+1000) || s_rsp_resp!==(2'(got_responses)))
      $fatal(1,"CDC response mismatch/loss/order at %0d epoch=%0d",got_responses,epoch);
    got_responses=got_responses+1;
  end
  task automatic reset_both;
    #1;s_rst_n=0;m_rst_n=0;s_req_valid=0;m_rsp_valid=0;
    drain_requests=0;drain_responses=0;
    repeat(6)@(negedge s_clk);
    sent_requests=0;got_requests=0;sent_responses=0;got_responses=0;
    // Both resets come from shell reset with independent synchronous release.
    @(negedge m_clk);m_rst_n=1;
    repeat(3)@(negedge s_clk);s_rst_n=1;
    repeat(5)@(negedge s_clk);
    if(m_req_valid || s_rsp_valid)$fatal(1,"Stale FIFO contents after reset");
  endtask
  task automatic send_requests(input integer count);
    for(integer i=0;i<count;i=i+1)begin
      @(negedge s_clk);s_req_addr=sent_requests*64;s_req_write=sent_requests%2;
      s_req_wdata=pattern(sent_requests);s_req_wstrb=64'h9a5512ff1234a55a^sent_requests;s_req_valid=1;
      do @(posedge s_clk);while(!s_req_ready);
      sent_requests=sent_requests+1;
      @(negedge s_clk);s_req_valid=0;
    end
  endtask
  task automatic send_responses(input integer count);
    for(integer i=0;i<count;i=i+1)begin
      @(negedge m_clk);m_rsp_rdata=pattern(sent_responses+1000);m_rsp_resp=sent_responses;m_rsp_valid=1;
      do @(posedge m_clk);while(!m_rsp_ready);
      sent_responses=sent_responses+1;
      @(negedge m_clk);m_rsp_valid=0;
    end
  endtask
  initial begin #3000000;$fatal(1,"CDC simulation timeout");end
  initial begin
    reset_both();
    // Fill both depth-16 queues with readers stopped and prove backpressure.
    fork send_requests(16);send_responses(16);join
    repeat(10)@(negedge s_clk);
    if(s_req_ready || m_rsp_ready)$fatal(1,"Full FIFO did not backpressure writer");
    drain_requests=1;drain_responses=1;
    fork send_requests(100);send_responses(100);join
    wait(got_requests==116 && got_responses==116);
    repeat(10)@(negedge s_clk);
    if(m_req_valid || s_rsp_valid)$fatal(1,"FIFO duplicated data");
    // Flush live queued data; epoch tags catch stale data across reset.
    drain_requests=0;drain_responses=0;
    fork send_requests(9);send_responses(9);join
    epoch=2;reset_both();drain_requests=1;drain_responses=1;
    fork send_requests(64);send_responses(64);join
    wait(got_requests==64 && got_responses==64);
    $display("PASS: CDC independent clocks full/backpressure wrap ordering reset flush request609 response514");
    $finish;
  end
endmodule

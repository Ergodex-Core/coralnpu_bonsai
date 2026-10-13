`timescale 1ns / 1ps
module tb;
  parameter integer ENGINES = 2;
  parameter integer LOOKUP = 1;
  reg clk = 0;
  always #5 clk = ~clk;
  reg rst_n = 0, job_valid = 0, rd_ready = 0, rsp_valid = 0, rsp_error = 0, out_ready = 0;
  reg [  3:0] job_m;
  reg [  5:0] job_n;
  reg [  7:0] job_lanes;
  reg [127:0] rsp_data = 0;
  reg [  3:0] rsp_id = 0;
  wire job_ready, rd_valid, rsp_ready, out_valid, done, job_error;
  wire [31:0] rd_addr, tag, cycles_total, cycles_load, bytes_read;
  wire [3:0] rd_id, token;
  wire [5:0] row_base;
  wire [ENGINES*32-1:0] dots;
  wire [ENGINES*16-1:0] scales;
  wire [ENGINES-1:0] mask, error_bits;
  bonsai_matmul_tile #(
      .ENGINES(ENGINES),
      .LOOKUP (LOOKUP)
  ) dut (
      .clk(clk),
      .rst_n(rst_n),
      .job_valid(job_valid),
      .job_ready(job_ready),
      .job_m(job_m),
      .job_n(job_n),
      .job_lanes(job_lanes),
      .job_tag(32'h103),
      .weight_base(32'd0),
      .activation_base(32'd1088),
      .rd_valid(rd_valid),
      .rd_ready(rd_ready),
      .rd_addr(rd_addr),
      .rd_id(rd_id),
      .rsp_valid(rsp_valid),
      .rsp_ready(rsp_ready),
      .rsp_data(rsp_data),
      .rsp_id(rsp_id),
      .rsp_error(rsp_error),
      .out_valid(out_valid),
      .out_ready(out_ready),
      .out_dots(dots),
      .out_scales(scales),
      .out_mask(mask),
      .out_error(error_bits),
      .out_tag(tag),
      .out_token(token),
      .out_row_base(row_base),
      .done(done),
      .job_error(job_error),
      .cycles_total(cycles_total),
      .cycles_load(cycles_load),
      .bytes_read(bytes_read)
  );
  reg [127:0] memory[0:131];
  reg [63:0] expected[0:255];
  reg pending[0:15];
  integer due[0:15];
  reg [31:0] addr[0:15];
  reg [255:0] seen;
  integer m, n, lanes, inject, expected_job_error, reset_test;
  integer cycle, accepted_reads, outputs, out_stall, request_stall, start_cycle, last_cycle;
  integer i, pick, id, idx, seen_count;
  reg held;
  reg [ENGINES*32+ENGINES*16+ENGINES*2+42-1:0] held_payload;
  wire [ENGINES*32+ENGINES*16+ENGINES*2+42-1:0] payload = {
    dots, scales, mask, error_bits, tag, token, row_base
  };
  string memory_file, expected_file;
  initial begin
    if (!$value$plusargs("MEM=%s", memory_file)) $fatal(1, "MEM missing");
    if (!$value$plusargs("EXPECTED=%s", expected_file)) $fatal(1, "EXPECTED missing");
    if (!$value$plusargs("M=%d", m)) m = 1;
    if (!$value$plusargs("N=%d", n)) n = 32;
    if (!$value$plusargs("LANES=%d", lanes)) lanes = 128;
    if (!$value$plusargs("INJECT=%d", inject)) inject = 0;
    if (!$value$plusargs("JOB_ERROR=%d", expected_job_error)) expected_job_error = 0;
    if (!$value$plusargs("RESET_TEST=%d", reset_test)) reset_test = 0;
    $readmemh(memory_file, memory, 0, 67 + m * 8);
    $readmemh(expected_file, expected, 0, m * n - 1);
    job_m = m;
    job_n = n;
    job_lanes = lanes;
    seen = 0;
    seen_count = 0;
    held = 0;
    for (i = 0; i < 16; i = i + 1) pending[i] = 0;
    repeat (16) @(negedge clk);
    rst_n = 1;
    job_valid = 1;
    @(negedge clk);
    job_valid = 0;
    if (reset_test != 0) begin
      // Cancel the old job and flush the external memory epoch before restart.
      rd_ready = 1;
      repeat (6) @(negedge clk);
      rst_n = 0;
      rd_ready = 0;
      rsp_valid = 0;
      repeat (16) @(negedge clk);
      rst_n = 1;
      job_valid = 1;
      @(negedge clk);
      job_valid = 0;
    end
    cycle = 0;
    accepted_reads = 0;
    outputs = 0;
    out_stall = 0;
    request_stall = 0;
    while (cycle < 10000) begin
      rd_ready  = (cycle % 16) < 12;
      out_ready = (cycle % 16) < 12;
      if (!rsp_valid) begin
        pick = -1;
        // Highest ready ID deliberately permits out-of-order responses.
        for (i = 0; i < 16; i = i + 1) if (pending[i] && due[i] <= cycle) pick = i;
        if (pick >= 0) begin
          rsp_valid = 1;
          rsp_id = pick;
          rsp_data = memory[addr[pick]/16];
          rsp_error = inject && addr[pick] == 0;
        end
      end
      @(posedge clk);
      if (held && (!out_valid || payload !== held_payload))
        $fatal(1, "Output changed under backpressure");
      held = out_valid && !out_ready;
      held_payload = payload;
      if (rd_valid && !rd_ready) request_stall = request_stall + 1;
      if (out_valid && !out_ready) out_stall = out_stall + 1;
      if (rd_valid && rd_ready) begin
        if (pending[rd_id]) $fatal(1, "ID reused while pending");
        if (rd_addr % 16 || rd_addr >= 1088 + m * 128) $fatal(1, "Bad read address");
        pending[rd_id] = 1;
        addr[rd_id] = rd_addr;
        due[rd_id] = cycle + 8;
        accepted_reads = accepted_reads + 1;
      end
      if (rsp_valid && rsp_ready) begin
        if (!pending[rsp_id]) $fatal(1, "Unexpected response");
        pending[rsp_id] = 0;
      end
      if (out_valid && out_ready) begin
        if (tag !== 32'h103) $fatal(1, "Wrong job tag");
        for (i = 0; i < ENGINES; i = i + 1) begin
          if (mask[i] !== (row_base + i < n)) $fatal(1, "Wrong row mask");
          if (mask[i]) begin
            idx = token * n + row_base + i;
            if (idx >= m * n || seen[idx]) $fatal(1, "Duplicate/out-of-range output");
            if(dots[i*32+:32]!==expected[idx][31:0] || scales[i*16+:16]!==expected[idx][47:32] || error_bits[i]!==expected[idx][48])begin
              $display("idx=%0d dot=%0d expected=%0d scale=%h expected=%h err=%b expected=%b", idx,
                       $signed(dots[i*32+:32]), $signed(expected[idx][31:0]), scales[i*16+:16],
                       expected[idx][47:32], error_bits[i], expected[idx][48]);
              $fatal(1, "Oracle mismatch");
            end
            seen[idx]  = 1;
            seen_count = seen_count + 1;
          end
        end
        outputs = outputs + 1;
      end
      // Update driven response only after the sampled rising edge.
      #1;
      if (rsp_valid && rsp_ready) rsp_valid = 0;
      if (done) begin
        if(seen_count!=m*n || accepted_reads!=68+m*8 || bytes_read!=(68+m*8)*16 || job_error!==expected_job_error[0])
          $fatal(1, "Completion/count/error mismatch");
        for (i = 0; i < 16; i = i + 1) if (pending[i]) $fatal(1, "Undrained request");
        $display(
            "RESULT engines=%0d lookup=%0d M=%0d N=%0d lanes=%0d cycles=%0d load_cycles=%0d bytes=%0d outputs=%0d request_stall=%0d output_stall=%0d exact_values=%0d reset_test=%0d",
            ENGINES, LOOKUP, m, n, lanes, cycles_total, cycles_load, bytes_read, outputs,
            request_stall, out_stall, seen_count, reset_test);
        $finish;
      end
      @(negedge clk);
      cycle = cycle + 1;
    end
    $fatal(1, "Cycle limit");
  end
endmodule

// Bounded standalone one-K-group tile feeder. This is NOT a Coral AXI client.
// Loads one slab32 PQ2 group and up to eight activation rows over a tagged bus.
module bonsai_matmul_tile #(
    parameter integer ENGINES = 2,
    parameter integer LOOKUP  = 1
) (
    input wire clk,
    input wire rst_n,
    input wire job_valid,
    output wire job_ready,
    input wire [3:0] job_m,
    input wire [5:0] job_n,
    input wire [7:0] job_lanes,
    input wire [31:0] job_tag,
    input wire [31:0] weight_base,
    input wire [31:0] activation_base,
    output wire rd_valid,
    input wire rd_ready,
    output wire [31:0] rd_addr,
    output wire [3:0] rd_id,
    input wire rsp_valid,
    output wire rsp_ready,
    input wire [127:0] rsp_data,
    input wire [3:0] rsp_id,
    input wire rsp_error,
    output wire out_valid,
    input wire out_ready,
    output wire [ENGINES*32-1:0] out_dots,
    output wire [ENGINES*16-1:0] out_scales,
    output wire [ENGINES-1:0] out_mask,
    output wire [ENGINES-1:0] out_error,
    output wire [31:0] out_tag,
    output wire [3:0] out_token,
    output wire [5:0] out_row_base,
    output reg done,
    output reg job_error,
    output reg [31:0] cycles_total,
    output reg [31:0] cycles_load,
    output reg [31:0] bytes_read
);
  localparam IDLE = 0, LOAD = 1, ACT = 2, ISSUE = 3, WAIT_OUT = 4;
  reg [2:0] state;
  reg [3:0] m, token;
  reg [5:0] n, row_base;
  reg [7:0] lanes;
  reg [31:0] tag, wb, ab;
  reg [7:0] issued, completed, requests;
  reg [15:0] occupied;
  reg [7:0] request_index[0:15];
  reg [8191:0] weights;
  reg [511:0] scales;
  reg [8191:0] activations;
  reg fault;
  wire a_ready, in_ready, cluster_valid;
  wire [ENGINES-1:0] cluster_error;
  reg [ENGINES*256-1:0] codes_in;
  reg [ENGINES*16-1:0] scales_in;
  reg [ENGINES-1:0] mask_in;
  integer e;
  always @* begin
    codes_in  = 0;
    scales_in = 0;
    mask_in   = 0;
    for (e = 0; e < ENGINES; e = e + 1) begin
      if ({26'b0, row_base} + e < n) begin
        codes_in[e*256+:256] = weights[({26'b0, row_base}+e)*256+:256];
        scales_in[e*16+:16] = scales[({26'b0, row_base}+e)*16+:16];
        mask_in[e] = 1;
      end
    end
  end
  assign job_ready = state == IDLE;
  assign rd_valid = state == LOAD && issued < requests && !occupied[issued[3:0]];
  assign rd_id = issued[3:0];
  assign rd_addr = issued < 68 ? wb + {24'b0, issued} * 16 : ab + ({24'b0, issued} - 68) * 16;
  assign rsp_ready = state == LOAD;
  assign out_valid = cluster_valid && state == WAIT_OUT;
  assign out_error = cluster_error | ({ENGINES{fault}} & out_mask);
  assign out_token = token;
  assign out_row_base = row_base;

  bonsai_dot_cluster #(
      .ENGINES(ENGINES),
      .LOOKUP (LOOKUP)
  ) cluster (
      .clk(clk),
      .rst_n(rst_n),
      .a_valid(state == ACT),
      .a_ready(a_ready),
      .a_data(activations[token*1024+:1024]),
      .a_lanes(lanes),
      .a_tag({28'b0, token}),
      .in_valid(state == ISSUE),
      .in_ready(in_ready),
      .in_codes(codes_in),
      .in_scales(scales_in),
      .in_mask(mask_in),
      .in_tag(tag),
      .out_valid(cluster_valid),
      .out_ready(out_ready && state == WAIT_OUT),
      .out_dots(out_dots),
      .out_scales(out_scales),
      .out_mask(out_mask),
      .out_error(cluster_error),
      .out_tag(out_tag),
      .out_a_tag()
  );
  always @(posedge clk) begin
    if (!rst_n) begin
      state <= IDLE;
      done <= 0;
      job_error <= 0;
      fault <= 0;
      m <= 0;
      n <= 0;
      lanes <= 0;
      tag <= 0;
      wb <= 0;
      ab <= 0;
      token <= 0;
      row_base <= 0;
      issued <= 0;
      completed <= 0;
      requests <= 0;
      occupied <= 0;
      cycles_total <= 0;
      cycles_load <= 0;
      bytes_read <= 0;
    end else begin
      done <= 0;
      if (state != IDLE) cycles_total <= cycles_total + 1;
      if (state == LOAD) cycles_load <= cycles_load + 1;
      if (state == IDLE && job_valid) begin
        cycles_total <= 0;
        cycles_load <= 0;
        bytes_read <= 0;
        job_error <= 0;
        fault <= 0;
        occupied <= 0;
        issued <= 0;
        completed <= 0;
        token <= 0;
        row_base <= 0;
        if (job_m==0 || job_m>8 || job_n==0 || job_n>32 || job_lanes>128 ||
            weight_base[3:0]!=0 || activation_base[3:0]!=0 ||
            weight_base>32'hfffffbc0 || activation_base>32'hfffffc00) begin
          done <= 1;
          job_error <= 1;
        end else begin
          m <= job_m;
          n <= job_n;
          lanes <= job_lanes;
          tag <= job_tag;
          wb <= weight_base;
          ab <= activation_base;
          requests <= 68 + {4'b0, job_m} * 8;
          state <= LOAD;
        end
      end
      if (state == LOAD) begin
        if (rd_valid && rd_ready) begin
          occupied[rd_id] <= 1;
          request_index[rd_id] <= issued;
          issued <= issued + 1;
        end
        if (rsp_valid && rsp_ready) begin
          if (!occupied[rsp_id]) begin
            fault <= 1;
          end else begin
            occupied[rsp_id] <= 0;
            completed <= completed + 1;
            bytes_read <= bytes_read + 16;
            if (rsp_error) fault <= 1;
            if (request_index[rsp_id] < 64) weights[request_index[rsp_id]*128+:128] <= rsp_data;
            else if (request_index[rsp_id] < 68)
              scales[(request_index[rsp_id]-64)*128+:128] <= rsp_data;
            else activations[(request_index[rsp_id]-68)*128+:128] <= rsp_data;
          end
        end
        if (completed == requests) state <= ACT;
      end
      if (state == ACT && a_ready) state <= ISSUE;
      if (state == ISSUE && in_ready) state <= WAIT_OUT;
      if (out_valid && out_ready) begin
        if (|(out_error & out_mask)) job_error <= 1;
        if ({26'b0, row_base} + ENGINES >= n) begin
          row_base <= 0;
          if (token + 1 >= m) begin
            done <= 1;
            job_error <= job_error || fault || (|(out_error & out_mask));
            state <= IDLE;
          end else begin
            token <= token + 1;
            state <= ACT;
          end
        end else begin
          row_base <= row_base + 6'(ENGINES);
          state <= ISSUE;
        end
      end
    end
  end
endmodule

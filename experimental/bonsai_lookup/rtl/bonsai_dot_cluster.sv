// Independent Bonsai implementation inspired by TeLLMe's shared triple tables.
// See ../NOTICE.md for pinned sources and BSD-3-Clause attribution.
module bonsai_dot_cluster #(
    parameter integer ENGINES = 2,
    parameter integer LOOKUP  = 1
) (
    input wire clk,
    input wire rst_n,
    input wire a_valid,
    output wire a_ready,
    input wire [1023:0] a_data,
    input wire [7:0] a_lanes,
    input wire [31:0] a_tag,
    input wire in_valid,
    output wire in_ready,
    input wire [ENGINES*256-1:0] in_codes,
    input wire [ENGINES*16-1:0] in_scales,
    input wire [ENGINES-1:0] in_mask,
    input wire [31:0] in_tag,
    output reg out_valid,
    input wire out_ready,
    output reg [ENGINES*32-1:0] out_dots,
    output reg [ENGINES*16-1:0] out_scales,
    output reg [ENGINES-1:0] out_mask,
    output reg [ENGINES-1:0] out_error,
    output reg [31:0] out_tag,
    output reg [31:0] out_a_tag
);
  reg have_a;
  reg [1023:0] activation;
  reg [7:0] lanes;
  reg [31:0] activation_tag;
  reg signed [9:0] table_values[0:42][0:26];
  wire [ENGINES*32-1:0] dots;
  wire [ENGINES-1:0] errors;
  assign a_ready  = !out_valid;
  assign in_ready = have_a && !a_valid && (!out_valid || out_ready);

  function automatic signed [9:0] operand(input [1023:0] values, input integer lane,
                                          input [7:0] valid_lanes);
    reg signed [7:0] value;
    begin
      value = 0;
      if (lane < 128 && lane < valid_lanes) value = $signed(values[lane*8+:8]);
      operand = {{2{value[7]}}, value};
    end
  endfunction
  function automatic signed [9:0] term(input signed [9:0] value, input integer code);
    begin
      case (code)
        0: term = -value;
        1: term = 0;
        2: term = value;
        default: term = 0;
      endcase
    end
  endfunction
  integer t, index;
  always @(posedge clk) begin
    if (!rst_n) begin
      have_a <= 0;
      activation <= 0;
      lanes <= 0;
      activation_tag <= 0;
      out_valid <= 0;
      out_dots <= 0;
      out_scales <= 0;
      out_mask <= 0;
      out_error <= 0;
      out_tag <= 0;
      out_a_tag <= 0;
    end else begin
      if (out_valid && out_ready) out_valid <= 0;
      if (a_valid && a_ready) begin
        activation <= a_data;
        lanes <= a_lanes;
        activation_tag <= a_tag;
        have_a <= (a_lanes <= 128);
        if (LOOKUP != 0) begin
          for (t = 0; t < 43; t = t + 1) begin
            for (index = 0; index < 27; index = index + 1) begin
              table_values[t][index] <= term(operand(a_data, 3 * t, a_lanes), index % 3) +
                  term(operand(a_data, 3 * t + 1, a_lanes), (index / 3) % 3) +
                  term(operand(a_data, 3 * t + 2, a_lanes), index / 9);
            end
          end
        end
      end
      if (in_valid && in_ready) begin
        out_valid <= 1;
        out_dots <= dots;
        out_scales <= in_scales;
        out_mask <= in_mask;
        out_error <= errors & in_mask;
        out_tag <= in_tag;
        out_a_tag <= activation_tag;
      end
    end
  end

  genvar e, i, level, node;
  generate
    for (e = 0; e < ENGINES; e = e + 1) begin : engine
      wire [127:0] bad;
      for (i = 0; i < 128; i = i + 1) begin : invalid_code
        assign bad[i] = (i < lanes) && (in_codes[e*256+i*2+:2] == 2'b11);
      end
      assign errors[e] = |bad;
      if (LOOKUP != 0) begin : lookup_path
        wire signed [15:0] tree[0:126];
        for (i = 0; i < 64; i = i + 1) begin : leaf
          if (i < 43) begin : triple
            wire [1:0] c0, c1, c2;
            wire [5:0] address;
            wire signed [9:0] selected;
            assign c0 = (3 * i < lanes) ? in_codes[e*256+6*i+:2] : 2'b01;
            if (3 * i + 1 < 128) begin : second
              assign c1 = (3 * i + 1 < lanes) ? in_codes[e*256+(3*i+1)*2+:2] : 2'b01;
            end else assign c1 = 2'b01;
            if (3 * i + 2 < 128) begin : third
              assign c2 = (3 * i + 2 < lanes) ? in_codes[e*256+(3*i+2)*2+:2] : 2'b01;
            end else assign c2 = 2'b01;
            assign address = {4'b0, c0} + 6'd3 * {4'b0, c1} + 6'd9 * {4'b0, c2};
            assign selected = (address < 27) ? table_values[i][address[4:0]] : 10'sd0;
            assign tree[63+i] = {{6{selected[9]}}, selected};
          end else assign tree[63+i] = 0;
        end
        for (node = 0; node < 63; node = node + 1) begin : add
          assign tree[node] = tree[2*node+1] + tree[2*node+2];
        end
        assign dots[e*32+:32] = (!in_mask[e] || errors[e]) ? 32'b0 : {{16{tree[0][15]}}, tree[0]};
      end else begin : direct_path
        wire signed [15:0] tree[0:254];
        for (i = 0; i < 128; i = i + 1) begin : leaf
          wire signed [9:0] value;
          assign value = (i < lanes) ? term(
              operand(activation, i, lanes), {30'b0, in_codes[e*256+i*2+:2]}
          ) : 10'sd0;
          assign tree[127+i] = {{6{value[9]}}, value};
        end
        for (node = 0; node < 127; node = node + 1) begin : add
          assign tree[node] = tree[2*node+1] + tree[2*node+2];
        end
        assign dots[e*32+:32] = (!in_mask[e] || errors[e]) ? 32'b0 : {{16{tree[0][15]}}, tree[0]};
      end
    end
  endgenerate
endmodule

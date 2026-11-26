// Nominal manifold reference envelopes for Radxa ZERO 3E v1.2 + RA023 PoE HAT (14.5 mm pillars).
// Combines official RS109_RA023 3D STEP bounds with exact chamfered BG_OUTLINE PCB, real 40-pin
// GPIO centerline (CON1 y=[24.135,29.215], underside solder pads y<=28.771), and real underside &
// topside component footprints extracted from official radxa_zero_3e_v1200_bottom.dxf and
// radxa_zero_3e_v1200_top.dxf (omitted from Radxa's 3D STEP).
// Board-local frame: SBC PCB x=[0,65], y=[0,30], z=[-1.6,0]. Placed in housing by Z + 7.4 mm.
// Nominal design reference only — NOT measured owner hardware.
part = "sbc-ref";
hat_gap = 14.5;
$fn = 36;
mounts = [[3.549904,3.599942],[61.399928,3.599942],[3.599942,26.450036],[61.399928,26.500074]];

module box_xy(x0, x1, y0, y1, z0, z1) {
  translate([x0, y0, z0]) cube([x1 - x0, y1 - y0, z1 - z0]);
}

module sbc_ref() {
  union() {
    // Official chamfered 65 x 30 x 1.6 mm ZERO 3E v1.2 PCB (BG_OUTLINE ~2.2 mm corner chamfers)
    translate([0, 0, -1.6]) linear_extrude(1.6) polygon([
      [2.274, 0.001], [62.801, 0.001], [65.001, 2.212], [65.001, 27.779],
      [62.796, 30.005], [2.200, 30.005], [0.001, 27.800], [0.001, 22.470],
      [6.167, 22.025], [6.167, 8.025], [3.661, 7.571], [0.001, 7.571],
      [0.001, 2.040]
    ]);
    // Gigabit Ethernet RJ45 jack (official STEP outline y=6.93..23.13, z=-4.55..6.85)
    translate([-6.4, 6.93, -4.55]) cube([21.4, 16.20, 11.40]);
    // micro-HDMI receptacle (top-mount, covered by front wall, y=-1.5..4.5)
    translate([9.2, -1.5, 0]) cube([6.5, 6.0, 3.0]);
    // USB 3.0 Host Type-C receptacle (inner, center x=41.57)
    translate([41.57 - 4.5, -1.5, 0]) cube([9.0, 7.5, 3.16]);
    // USB 2.0 OTG / 5V Power Type-C receptacle (outer, center x=54.03)
    translate([54.03 - 4.5, -1.5, 0]) cube([9.0, 7.5, 3.16]);
    // Real 40-pin GPIO male header (CON1 centerline y=26.6751, shroud y=[24.1351,29.2151], z=0..8.5)
    box_xy(7.00, 58.00, 24.135, 29.215, 0.0, 8.50);
    // Main RK3566 SoC / PMIC / DRAM nominal envelope on top face
    translate([22.0, 8.0, 0]) cube([24.0, 14.0, 2.2]);

    // Real top-side front & closed-end SMD keepouts from radxa_zero_3e_v1200_top.dxf:
    box_xy(17.00, 21.01, 1.15, 4.50, 0.0, 1.50);  // SMD east of micro-HDMI
    box_xy(21.01, 24.10, 0.97, 4.50, 0.0, 1.50);  // U6 QFN & adjacent pads (y>=0.974)
    box_xy(24.30, 28.90, 0.55, 4.50, 0.0, 2.00);  // Two top power inductors
    box_xy(28.90, 30.80, 1.00, 4.50, 0.0, 1.50);  // Passives between inductor and WLCSP
    box_xy(30.90, 36.80, 0.55, 4.50, 0.0, 1.50);  // Top WLCSP/BGA + passives
    box_xy(58.50, 63.62, 7.00, 23.00, 0.0, 1.50); // C340..C348 & closed-end top SMD (x<=63.619)

    // Real underside STEP components:
    box_xy(11.79, 16.15, 8.11, 24.15, -4.30, -1.60); // Four underside power inductors L1..L4
    box_xy(51.92, 67.50, 9.50, 20.50, -5.26, -1.60); // Underside microSD cage + seated card
    box_xy(59.81, 65.00, 7.49, 22.59, -4.46, -1.60); // Underside CSI connector
    box_xy(17.00, 51.00, 7.00, 23.50, -4.20, -1.60); // Mid-board underside SMD keepout

    // Real underside front components & THT solder legs from radxa_zero_3e_v1200_bottom.dxf:
    box_xy(8.49, 10.17, 0.89, 5.50, -3.40, -1.60);   // micro-HDMI left THT shield legs
    box_xy(14.70, 16.20, 0.80, 5.50, -3.40, -1.60);  // micro-HDMI right THT shield legs
    box_xy(10.20, 14.60, 3.80, 6.40, -2.80, -1.60);  // ED9-12, R140, R141
    box_xy(17.30, 19.60, 1.10, 3.40, -2.80, -1.60);  // M3 pad / underside front passive
    box_xy(19.80, 26.20, 1.45, 6.40, -3.00, -1.60);  // C202, C208, C209, 6R8, C27, R2, R5, R104, R106
    box_xy(26.30, 33.40, 0.50, 6.40, -4.10, -1.60);  // U12 (16-pin SOIC, ~2 mm), Q4, R71, 6R7, 6C6, R110-112
    box_xy(33.40, 45.20, 0.60, 6.80, -3.60, -1.60);  // 6C2-5, 6R2-5, C78, R48, U11, R70
    box_xy(45.30, 50.26, 0.65, 5.80, -3.40, -1.60);  // USB3 & USB-OTG left THT shield legs + C151
    box_xy(51.80, 58.60, 1.65, 6.80, -3.40, -1.60);  // C338, D4, L9, D5, Q7, C339
    box_xy(54.39, 58.60, 1.18, 6.80, -3.40, -1.60);  // R108, R109 & USB-OTG right THT shield leg
    // Real 40-pin GPIO underside through-hole solder pads (PIN_SOLDERMASK_BOTTOM y<=28.7706)
    box_xy(7.00, 58.00, 24.40, 28.771, -3.80, -1.60);
  }
}

module hat_ref() {
  union() {
    // 65 x 30 x 1.6 mm RA023 PoE HAT PCB at official 14.5 mm pillar spacing
    translate([0, 0, hat_gap]) cube([65, 30, 1.6]);
    // Female GPIO stack header between SBC and HAT (aligned to CON1 y=24.135..29.215)
    box_xy(7.00, 58.00, 24.135, 29.215, 8.55, hat_gap);
    // Optional 20 x 20 x 6.5 mm underside cooling fan envelope between boards
    translate([22.0, 3.5, hat_gap - 6.5]) cube([20.0, 20.0, 6.5]);
    // PoE transformer / inductor / rectifier tallest top-side envelope (to z_rel = hat_gap + 13.7)
    translate([16.5, 3.0, hat_gap + 1.6]) cube([29.0, 22.0, 12.1]);
    // Four M2.5 x 14.5 mm insulated copper pillars + upper CM2.5x4 screw head envelopes
    for (m = mounts) {
      translate([m[0], m[1], 0.05]) cylinder(d=5.2, h=hat_gap - 0.05);
      translate([m[0], m[1], hat_gap + 1.6]) cylinder(d=5.2, h=1.8);
    }
  }
}

if (part == "sbc-ref") sbc_ref();
else if (part == "hat-ref") hat_ref();
else assert(false, "Unknown reference part");

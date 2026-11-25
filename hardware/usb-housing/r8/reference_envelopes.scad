// Nominal manifold reference envelopes for Radxa ZERO 3E + RA023 PoE HAT (14.5 mm pillars).
// Derived from hardware/usb-housing/poe-reference.json (official RS109_RA023 STEP bounds)
// and Radxa's RA023 BOM (4 x M2.5 x 14.5 mm insulated copper pillars + 8 x CM2.5 x 4 screws).
// Board-local frame: SBC PCB x=[0,65], y=[0,30], z=[-1.6,0]. Placed in housing by Z + 7.4 mm.
// Nominal design reference only — NOT measured owner hardware.
part = "sbc-ref";
hat_gap = 14.5;
$fn = 36;
mounts = [[3.549904,3.599942],[61.399928,3.599942],[3.599942,26.450036],[61.399928,26.500074]];

module sbc_ref() {
  union() {
    // 65 x 30 x 1.6 mm ZERO 3E PCB
    translate([0, 0, -1.6]) cube([65, 30, 1.6]);
    // Gigabit Ethernet RJ45 jack (official face outline y=6.93..23.13, z=-4.55..6.85)
    translate([-6.4, 6.93, -4.55]) cube([21.4, 16.20, 11.40]);
    // micro-HDMI receptacle (covered by front wall, y=-1.5..4.5)
    translate([9.2, -1.5, 0]) cube([6.5, 6.0, 3.0]);
    // USB 3.0 Host Type-C receptacle (inner, center x=41.57)
    translate([41.57 - 4.5, -1.5, 0]) cube([9.0, 7.5, 3.16]);
    // USB 2.0 OTG / 5V Power Type-C receptacle (outer, center x=54.03)
    translate([54.03 - 4.5, -1.5, 0]) cube([9.0, 7.5, 3.16]);
    // 40-pin GPIO male header (top of SBC, z=0..8.5)
    translate([7.0, 23.7, 0]) cube([51.0, 5.1, 8.5]);
    // Main RK3566 SoC / PMIC / DRAM nominal envelope on top face
    translate([22.0, 8.0, 0]) cube([24.0, 14.0, 2.2]);
    // Underside microSD cage + seated card (2.5 mm projection past x=65)
    translate([52.0, 9.5, -3.25]) cube([15.5, 11.0, 1.65]);
    // Lowest underside through-hole / SMD nominal keepout (z_min = -5.26 from official STEP)
    translate([17.0, 8.0, -5.26]) cube([28.0, 14.0, 3.66]);
  }
}

module hat_ref() {
  union() {
    // 65 x 30 x 1.6 mm RA023 PoE HAT PCB at official 14.5 mm pillar spacing
    translate([0, 0, hat_gap]) cube([65, 30, 1.6]);
    // Female GPIO stack header between SBC and HAT (kept inside y=23.7..28.8)
    translate([7.0, 23.7, 8.55]) cube([51.0, 5.1, hat_gap - 8.55]);
    // Optional 20 x 20 x 6.5 mm underside cooling fan envelope between boards
    // (shifted 0.5 mm clear of 40-pin GPIO header y=23.7..28.8).
    translate([22.0, 3.5, hat_gap - 6.5]) cube([20.0, 20.0, 6.5]);
    // PoE transformer / inductor / rectifier tallest top-side envelope (to z_rel = hat_gap + 13.7)
    translate([16.5, 3.0, hat_gap + 1.6]) cube([29.0, 22.0, 12.1]);
    // Four M2.5 x 14.5 mm insulated copper pillars + upper CM2.5x4 screw head envelopes
    // (lower CM2.5x4 screw heads sit below SBC PCB at z=-3.4..-1.6, kept inside dia-8 reserve)
    for (m = mounts) {
      translate([m[0], m[1], 0.05]) cylinder(d=5.2, h=hat_gap - 0.05);
      translate([m[0], m[1], hat_gap + 1.6]) cylinder(d=5.2, h=1.8);
    }
  }
}

if (part == "sbc-ref") sbc_ref();
else if (part == "hat-ref") hat_ref();
else assert(false, "Unknown reference part");

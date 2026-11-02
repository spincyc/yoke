// Yoke USB endpoint housing R1, millimetres. Radxa ZERO 3E V1.2.
// Source coordinates preserve the official rs109_pcba.stp board frame:
// PCB x=0..65, y=0..30, top z=0, bottom z=-1.6.
// Print each half with its outside face on the bed; no supports.
part = "plate"; // base, lid, plate, assembled, exploded
$fn = 64;
pcb_top = 8.5;
pcb_thickness = 1.6;
pad_finish = 0.02; // official STEP mounting-pad faces stand 0.02 mm proud
floor_thickness = 2;
height = 20;
seam = 10;
closure_gap = 0.15; // permits corner-pad clamping before the walls bottom out
wall = 2;
clearance = 0.4;
x0 = -9;
x1 = 67.4;
y0 = -2.4;
y1 = 32.4;
mounts = [[3.549904,3.599942],[61.399928,3.599942],
          [3.599942,26.450036],[61.399928,26.500074]];
lid_through_d = 3.0; // matches hand-bit cleanup of the lid only
through_d = 2.9; // M2.5 screw clearance; PCB holes are nominal 2.8
pad_d = 4.6; // only bare corner mounting regions, never IC packages
nut_af = 5.25; // M2.5 ISO4032 nominal AF5, first-article fit clearance
nut_depth = 2.2;

module outline(z,h) { translate([x0,y0,z]) cube([x1-x0,y1-y0,h]); }
module hollow(z,h) {
    difference() {
        outline(z,h);
        translate([x0+wall,y0+wall,z-0.01])
            cube([x1-x0-2*wall,y1-y0-2*wall,h+0.02]);
    }
}
module ports() {
    // Full lower-edge RJ45 mouth: no lip under the plug latch.
    translate([x0-1,5,-1]) cube([12,20,18]);
    // Shared seam-opening cable bay for micro-HDMI and both USB-C ports.
    // Broad access also accommodates cable overmoulds and leaves no bridge.
    translate([7,y0-1,6]) cube([53.5,4.0,8.5]);
    // microSD/CSI service at the far end; SD insertion is below the PCB.
    translate([64,7,3.5]) cube([5,16,8.5]);
}
module vents(z) {
    // 3 mm through-slots with broad, connected ribs; away from corner pads.
    for (x=[16:6:52]) translate([x,8,z]) cube([3,13,4]);
}
module holes(z,h,d=through_d) {
    for (p=mounts) translate([p[0],p[1],z]) cylinder(d=d,h=h);
}
module base() {
    difference() {
        union() {
            outline(0,floor_thickness);
            hollow(0,seam);
            for(p=mounts) {
                translate([p[0],p[1],0]) cylinder(d=8,h=3);
                translate([p[0],p[1],0]) cylinder(d=pad_d,h=pcb_top-pcb_thickness-pad_finish);
            }
        }
        ports();
        vents(-1);
        holes(-1,height+2);
        // R1 identity is recessed into the exterior; no added plate bodies.
        translate([24,27.5,-0.01]) linear_extrude(0.45)
            mirror([1,0,0]) text("YOKE R1 B",size=2.2,halign="center");
    }
}
module lid_assembled() {
    difference() {
        union() {
            outline(height-floor_thickness,floor_thickness);
            hollow(seam+closure_gap,height-seam-closure_gap);
            for(p=mounts) {
                translate([p[0],p[1],height-3]) cylinder(d=8,h=3);
                translate([p[0],p[1],pcb_top+pad_finish])
                    cylinder(d=pad_d,h=height-pcb_top-pad_finish);
            }
        }
        ports();
        vents(height-3);
        holes(-1,height+2,lid_through_d);
        // Flat, broad nut bearing ledge at z17.8 around the3mm bore. The printed variant
        // closes only the screw bore with a 0.20mm removable diaphragm.
        for(p=mounts) translate([p[0],p[1],height-nut_depth])
            rotate([0,0,30]) cylinder(d=nut_af/cos(30),h=nut_depth+1,$fn=6);
        translate([24,27.5,height-0.45]) linear_extrude(0.5)
            text("YOKE R1 L",size=2.2,halign="center");
    }
}
module lid(manufactured=false) {
    translate([0,30,height]) rotate([180,0,0]) union() {
        lid_assembled();
        if(manufactured) for(p=mounts)
            translate([p[0],p[1],height-nut_depth-0.2])
                cylinder(d=lid_through_d+0.4,h=0.2);
    }
}
if (part == "base") base();
else if (part == "lid") lid();
else if (part == "lid-printed") lid(true);
else if (part == "plate") { base(); translate([0,42,0]) lid(true); }
else if (part == "assembled") { base(); lid_assembled(); }
else if (part == "exploded") { base(); translate([0,0,17]) lid_assembled(); }
else assert(false,"Unknown part selector");

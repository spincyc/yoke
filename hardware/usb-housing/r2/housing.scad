// Yoke USB endpoint housing R2: canonical brand, unchanged R1 mechanics.
// Millimetres, same Radxa ZERO 3E V1.2 coordinate frame as ../housing.scad.
// The mechanical modules are imported, never copied or changed here.
use <../housing.scad>

part = "plate"; // base, lid, lid-printed, lid-assembled, plate, assembled, exploded, mark
$fn = 64;
mark_width = 8;
mark_depth = 0.45;
mark_center = [9,15];
brand_asset = "../../../site/assets/yoke-mark.svg";
id_depth = 0.45;
id_size = 3;

assert(mark_width > 0 && mark_width <= 8,
       "Larger marks require a new placement/clearance review");
assert(mark_depth > 0 && mark_depth <= 0.45,
       "Keep at least 1.55 mm of the 2 mm roof below the brand recess");
assert(id_depth > 0 && id_depth <= 0.45);

module mark_2d() {
    // The SVG's 24-unit visible width becomes 8 mm. Import preserves its
    // canonical path and flips the SVG Y axis into the CAD XY plane.
    resize([mark_width,0],auto=true) import(brand_asset,center=true);
}

module part_id(s) {
    text(s,size=id_size,font="DejaVu Sans Mono:style=Bold",halign="center");
}

module r2_base() {
    difference() {
        union() {
            base();
            // Restore only the shallow old R1 identity pocket. This patch
            // lies wholly within the existing 2 mm floor, clear of vents.
            translate([10,26.5,0]) cube([30,4.5,0.46]);
        }
        translate([24,27,-0.01]) linear_extrude(id_depth+0.01)
            mirror([1,0,0]) part_id("R2 B");
    }
}

module lid_id_restore() {
    // Same restoration on the outside roof; no interior material is added.
    // Nut wells and their bearing ledges are untouched.
    translate([10,26.5,19.54]) cube([30,4.5,0.46]);
}

module lid_engraving() {
    translate([24,27,20-id_depth]) linear_extrude(id_depth+0.01)
        part_id("R2 L");
    translate([mark_center[0],mark_center[1],20-mark_depth])
        linear_extrude(mark_depth+0.01) mark_2d();
}

module r2_lid_assembled() {
    difference() {
        union() { lid_assembled(); lid_id_restore(); }
        lid_engraving();
    }
}

module r2_lid(manufactured=false) {
    // Identical finished-lid-to-print transform and four sacrificial bore
    // membranes as R1. The finished meshes omit those cleanup membranes.
    difference() {
        union() {
            lid(manufactured);
            translate([0,30,20]) rotate([180,0,0]) lid_id_restore();
        }
        translate([0,30,20]) rotate([180,0,0]) lid_engraving();
    }
}

// A single neutral render material; STL exports carry geometry only.
color([0.26,0.29,0.27]) {
    if (part == "base") r2_base();
    else if (part == "lid") r2_lid();
    else if (part == "lid-printed") r2_lid(true);
    else if (part == "lid-assembled") r2_lid_assembled();
    else if (part == "plate") { r2_base(); translate([0,42,0]) r2_lid(true); }
    else if (part == "assembled") { r2_base(); r2_lid_assembled(); }
    else if (part == "exploded") { r2_base(); translate([0,0,17]) r2_lid_assembled(); }
    else if (part == "mark") linear_extrude(mark_depth) mark_2d();
    else assert(false,"Unknown part selector");
}

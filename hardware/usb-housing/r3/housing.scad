// Yoke USB enclosure R3: registered seam, captive metal nuts, recessed heads.
// Millimetres. Radxa ZERO 3E V1.2 official board frame, PCB top at z=8.5.
part = "plate";
$fn = 64;
height=20; floor_t=2; roof_t=2; seam=10; seam_gap=0.15;
body=[-10.6,69,-4,34]; inside=[-7,65.4,-0.4,30.4];
pcb_top=8.5; lower_pad=6.88; upper_pad=8.52;
mounts=[[3.549904,3.599942],[61.399928,3.599942],
        [3.599942,26.450036],[61.399928,26.500074]];
// Ear frame: local +Y points outboard, local X spans nut flats.
ears=[[-11.2,0,90],[-11.2,30,90],[69.6,0,-90],[69.6,30,-90]];
ear_r=4.8; bore_d=3; head_d=5.8; head_seat=2.4;
nut_af=5.25; nut_seat=15.4; nut_slot_h=2.25; tip_roof=19.2;
keeper_w=6.4; keeper_inner=3.15; keeper_top=17.85;
membrane=0.20;
keys=[[-2,-2.2,8],[15,32.2,10],[47,32.2,10]];
key_w=1.4; key_h=1.35; key_side_clear=0.25; key_top_clear=0.35;
pin_d=2.35; pin_top=8.8; pin_chamfer=0.25;
pin_angle=atan2(mounts[3][1]-mounts[0][1],mounts[3][0]-mounts[0][0]);
brand_asset="../../../site/assets/yoke-mark.svg";

module slab(bounds,z,h) {
    translate([bounds[0],bounds[2],z])
        cube([bounds[1]-bounds[0],bounds[3]-bounds[2],h]);
}
module ear_frame(e) { translate([e[0],e[1],0]) rotate([0,0,e[2]]) children(); }
module rounded_strip(w,d,r=0.4) {
    hull() for(x=[-w/2+r,w/2-r]) for(y=[r,d-r])
        translate([x,y]) circle(r=r);
}
module ear_outline() {
    hull() {
        // The clipped arc lies entirely inside the flat strip. Removing its
        // redundant tangent point avoids collinear export facets where the
        // keeper joins the ear, without changing the convex outer profile.
        intersection() {
            circle(r=ear_r);
            translate([-ear_r-1,-ear_r-1]) square([2*ear_r+2,2*ear_r+0.7]);
        }
        translate([0,keeper_inner]) rounded_strip(keeper_w,ear_r-keeper_inner);
    }
}
module walls(z,h) {
    difference() { slab(body,z,h); slab(inside,z-0.01,h+0.02); }
}
module ear_solid(z,h) {
    difference() {
        for(e=ears) ear_frame(e) translate([0,0,z]) linear_extrude(h) ear_outline();
        // Trim the complete ear union; no cylinder may intrude into the board cavity.
        slab(inside,max(z,floor_t),h+1);
    }
}
module ports() {
    // Cut the actual walls only. The old oversized cutter reached mounting posts.
    translate([body[0]-1,5,-1]) cube([(-6.9)-(body[0]-1),20,18]);
    translate([7,body[2]-1,6]) cube([53.5,0.2-(body[2]-1),8.5]);
    translate([65.3,7,3.5]) cube([body[1]-65.3+1,16,8.5]);
}
module vents(z) { for(x=[16:6:52]) translate([x,8,z]) cube([3,13,4]); }
module tongue(k) {
    translate([k[0],k[1],seam]) {
        translate([-k[2]/2,-key_w/2,0]) cube([k[2],key_w,key_h-0.3]);
        hull() {
            translate([-k[2]/2,-key_w/2,key_h-0.3]) cube([k[2],key_w,0.01]);
            translate([-k[2]/2+0.3,-key_w/2+0.3,key_h-0.01])
                cube([k[2]-0.6,key_w-0.6,0.01]);
        }
    }
}
module key_slot(k) {
    // 0.25 per side/end; 0.35 axial clearance above tongue, plus 0.15 wall gap.
    translate([k[0],k[1],seam+seam_gap-0.01]) {
        translate([-k[2]/2-key_side_clear,-key_w/2-key_side_clear,0])
            cube([k[2]+2*key_side_clear,key_w+2*key_side_clear,
                  key_h-seam_gap+key_top_clear+0.01]);
        hull() {
            translate([-k[2]/2-key_side_clear-0.3,-key_w/2-key_side_clear-0.3,-0.01])
                cube([k[2]+2*key_side_clear+0.6,key_w+2*key_side_clear+0.6,0.01]);
            translate([-k[2]/2-key_side_clear,-key_w/2-key_side_clear,0.3])
                cube([k[2]+2*key_side_clear,key_w+2*key_side_clear,0.01]);
        }
    }
}
module locator_pin(index) {
    p=mounts[index];
    translate([p[0],p[1],lower_pad]) intersection() {
        union() {
            cylinder(d=pin_d,h=pin_top-lower_pad-pin_chamfer);
            translate([0,0,pin_top-lower_pad-pin_chamfer])
                cylinder(d1=pin_d,d2=pin_d-0.5,h=pin_chamfer);
        }
        // The diagonal pin is relieved along the hole-centre baseline.
        if(index==3) rotate([0,0,pin_angle])
            translate([-0.8,-2,0]) cube([1.6,4,3]);
        else cylinder(d=pin_d+1,h=3);
    }
}
module keeper() {
    translate([0,keeper_inner,seam+seam_gap])
        linear_extrude(keeper_top-seam-seam_gap)
            rounded_strip(keeper_w,ear_r-keeper_inner);
}
module base(manufactured=false) {
    union() {
        difference() {
            union() {
                slab(body,0,floor_t); walls(0,seam);
                ear_solid(0,seam+seam_gap);
                for(p=mounts) translate([p[0],p[1],0]) cylinder(d=4.6,h=lower_pad);
                for(k=keys) tongue(k);
                for(e=ears) ear_frame(e) keeper();
                locator_pin(0); locator_pin(3);
            }
            ports(); vents(-1);
            for(e=ears) ear_frame(e) {
                translate([0,0,-1]) cylinder(d=head_d,h=head_seat+1);
                translate([0,0,head_seat-0.01]) cylinder(d=bore_d,h=height);
            }
            translate([26,28,-0.01]) linear_extrude(0.46)
                mirror([1,0,0]) text("R3 B",size=3,font="DejaVu Sans Mono:style=Bold",halign="center");
        }
        if(manufactured) for(e=ears) ear_frame(e)
            translate([0,0,head_seat]) cylinder(d=bore_d+0.4,h=membrane);
    }
}
module nut_channel() {
    translate([0,0,nut_seat]) {
        rotate([0,0,30]) cylinder(d=nut_af/cos(30),h=nut_slot_h,$fn=6);
        translate([-nut_af/2,0,0]) cube([nut_af,ear_r+1,nut_slot_h]);
    }
    // Outboard keeper tongue closes this opening only when the base is fitted.
    translate([-keeper_w/2-0.25,keeper_inner-0.25,seam+seam_gap-0.01])
        cube([keeper_w+0.5,ear_r-keeper_inner+1,keeper_top-(seam+seam_gap)+0.31]);
    // With the lid removed, a blunt 1.2 mm pin can push the nut out into a tray.
    translate([0,-5,16.45]) rotate([-90,0,0]) cylinder(d=1.5,h=5);
}
module lid_assembled(manufactured=false) {
    union() {
        difference() {
            union() {
                slab(body,height-roof_t,roof_t); walls(seam+seam_gap,height-seam-seam_gap);
                ear_solid(seam+seam_gap,height-seam-seam_gap);
                for(p=mounts) translate([p[0],p[1],upper_pad]) cylinder(d=4.6,h=height-upper_pad);
            }
            ports(); vents(height-3);
            for(k=keys) key_slot(k);
            for(e=ears) ear_frame(e) {
                translate([0,0,seam-1]) cylinder(d=bore_d,h=tip_roof-seam+1);
                nut_channel();
            }
            for(i=[0,3]) translate([mounts[i][0],mounts[i][1],upper_pad-0.01])
                cylinder(d=3,h=9.15-upper_pad+0.01);
            translate([9,15,19.55]) linear_extrude(0.46)
                resize([8,0],auto=true) import(brand_asset,center=true);
            translate([26,28,19.55]) linear_extrude(0.46)
                text("R3 L",size=3,font="DejaVu Sans Mono:style=Bold",halign="center");
        }
        if(manufactured) for(e=ears) ear_frame(e)
            translate([0,0,nut_seat-membrane]) cylinder(d=bore_d+0.4,h=membrane);
    }
}
module lid(manufactured=false) { translate([0,30,20]) rotate([180,0,0]) lid_assembled(manufactured); }
module coupon(which="base",manufactured=true) {
    // The actual front-left corner: ear, head, nut/keeper, seam key and round PCB pin.
    difference() {
        intersection() {
            if(which=="base") base(manufactured); else lid_assembled(manufactured);
            translate([-17,-6,-1]) cube([29,14,22]);
        }
        if(which=="base") translate([8,5,-0.01]) linear_extrude(0.31)
            mirror([1,0,0]) text("CB",size=2,font="DejaVu Sans Mono:style=Bold",halign="center");
        else translate([8,5,19.7]) linear_extrude(0.31)
            text("CL",size=2,font="DejaVu Sans Mono:style=Bold",halign="center");
    }
}
module screw() {
    // Maximum supplied screw envelope; smooth shank stands for real metal thread.
    union() {
        cylinder(d=2.5,h=16.35);
        translate([0,0,-2.12]) cylinder(d=5,h=2.12);
    }
}
module nut() { difference() {
    rotate([0,0,30]) cylinder(d=5/cos(30),h=2,$fn=6);
    translate([0,0,-0.1]) cylinder(d=2.5,h=2.2);
} }

if(part=="base") base();
else if(part=="base-printed") base(true);
else if(part=="lid") lid();
else if(part=="lid-printed") lid(true);
else if(part=="lid-assembled") lid_assembled();
else if(part=="plate") { base(true); translate([0,52,0]) lid(true); }
else if(part=="assembled" || part=="exploded") {
    color([0.65,0.68,0.65]) base();
    color([0.75,0.77,0.74]) translate([0,0,part=="exploded"?22:0]) lid_assembled();
    if(part=="assembled") for(e=ears) ear_frame(e) {
        color([0.3,0.3,0.3]) translate([0,0,head_seat]) screw();
        color([0.3,0.3,0.3]) translate([0,0,nut_seat]) nut();
    }
}
else if(part=="coupon-base") coupon("base",false);
else if(part=="coupon-lid") translate([0,30,20]) rotate([180,0,0]) coupon("lid",false);
else if(part=="coupon-base-printed") coupon("base",true);
else if(part=="coupon-lid-printed") translate([0,30,20]) rotate([180,0,0]) coupon("lid",true);
else if(part=="screw") screw();
else if(part=="nut") nut();
else assert(false,"Unknown part selector");

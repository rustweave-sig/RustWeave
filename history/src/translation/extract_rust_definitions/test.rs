fn train_or() {
    let INPUT: [[f64; 2]; 4] = [[0.0, 0.0], [0.0, 1.0], [1.0, 0.0], [1.0, 1.0]];
    let OUTPUT: [f64; 4] = [0.0, 1.0, 1.0, 1.0];
    static INITIALIZED: std::sync::Once = std::sync::Once::new();
    let mut ann = genann_init(2, 0, 0, 1);
    genann_randomize(&mut ann);

    for _ in 0..50 {
        for j in 0..4 {
            genann_train(&mut ann, &INPUT[j], &OUTPUT[j], 0.8);
        }
    }

    ann.activation_output = genann_act_threshold;
    lfequal(OUTPUT[0], *genann_run(&ann, &INPUT[0]));
    lfequal(OUTPUT[1], *genann_run(&ann, &INPUT[1]));
    lfequal(OUTPUT[2], *genann_run(&ann, &INPUT[2]));
    lfequal(OUTPUT[3], *genann_run(&ann, &INPUT[3]));

    genann_free(ann);
}
    



fn main() {
    let mut x = MIN;
    while x <= MAX {
        let y = sigmoid(x);
        println!("sigmoid({}) = {}", x, y);
        x += STEP;
    }
}
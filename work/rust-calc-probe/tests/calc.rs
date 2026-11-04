use rust_calc::{add, mul};

#[test]
fn add_works() {
    assert_eq!(add(1, 1), 2);
    assert_eq!(add(2, 3), 5);
}

#[test]
fn mul_works() {
    // weak: 1*1 == 1/1, so a `*` -> `/` mutant survives this assertion
    assert_eq!(mul(1, 1), 1);
}

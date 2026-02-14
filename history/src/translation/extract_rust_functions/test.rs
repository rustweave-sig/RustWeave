#[derive(Debug, Clone, PartialEq)]
struct List {
    value: f64,
    next: Option<Box<List>>,
    prev: Option<Box<List>>,
}
fn create(value: f64) -> Option<Box<List>> {
    Some(Box::new(List {
        value,
        next: None,
        prev: None,
    }))
}
fn delete(list: Option<Box<List>>, pos: i32) -> Option<Box<List>> {
    if list.is_none() || pos < 0 {
        return list;
    }
    let mut head = list.unwrap();
    if pos == 0 {
        if let Some(mut next) = head.next {
            next.prev = None;
            return Some(next);
        }
        return None;
    }
    let mut current = head.as_mut();
    let mut index = 0;
    let mut size = 0;
    while current.next.is_some() {
        size += 1;
        current = current.next.as_mut().unwrap();
    }
    if pos > size {
        return Some(head);
    }
    current = head.as_mut();
    while index < (pos - 1) as i32 && current.next.is_some() {
        index += 1;
        current = current.next.as_mut().unwrap();
    }
    if let Some(mut next) = current.next.take() {
        if let Some(next_next) = next.next.take() {
            current.next = Some(next_next);
            next_next.prev = Some(Box::new(current.clone()));
        }
    }
    Some(head)
}
fn insert(list: Option<Box<List>>, value: f64, pos: i32) -> Option<Box<List>> {
    if list.is_none() {
        return create(value);
    }
    if pos <= 0 {
        return list;
    }
    let mut head = list.unwrap();
    if pos == 1 {
        let mut new_node = create(value).unwrap();
        new_node.next = Some(head);
        head.prev = Some(new_node);
        return Some(new_node);
    }
    let mut current = head.as_mut();
    let mut index = 1;
    while index < (pos - 1) as i32 {
        if current.next.is_none() {
            break;
        }
        current = current.next.as_mut().unwrap();
        index += 1;
    }
    let mut new_node = Box::new(List {
        value,
        next: None,
        prev: None,
    });
    if current.next.is_some() {
        new_node.next = current.next.take();
        new_node.prev = Some(Box::new(*current));
        if let Some(next) = new_node.next.as_mut() {
            next.prev = Some(new_node);
        }
        current.next = Some(new_node);
    } else {
        new_node.prev = Some(Box::new(*current));
        current.next = Some(new_node);
    }
    Some(head)
}
fn search(list: Option<Box<List>>, value: f64) -> i32 {
    match list {
        None => 0,
        Some(node) => {
            if (node.value as f64 - value).abs() < f64::EPSILON {
                1
            } else {
                search(node.next, Some(value))
            }
        }
    }
}
fn print(list: Option<Box<List>>) {
    if let Some(node) = list {
        print!("{}\t", node.value);
        print(Some(node.next));
    }
}
fn example() {
    let mut my_list = create(0.0);
    my_list = insert(Some(my_list), 3.0, 1);
    my_list = insert(Some(my_list), 5.0, 3);
    my_list = insert(Some(my_list), 10.0, 3);
    my_list = insert(Some(my_list), 20.0, 3);
    print(Some(my_list.clone()));
    let searching = search(my_list.clone(), Some(Some(20.0)));
    println!("\n{}", searching);
    my_list = delete(Some(my_list), 1);
    my_list = delete(Some(my_list), 1);
    my_list = delete(Some(my_list), 1);
    my_list = delete(Some(my_list), 1);
    print(Some(my_list.clone()));
    let searching = search(my_list.clone(), Some(Some(20.0)));
    println!("\n{}", searching);
}
fn main() {
    example();
}

